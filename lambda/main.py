#!/usr/bin/env python

import os
from datetime import datetime, timezone, timedelta
import threading
import logging
import re

from botocore.vendored import requests
import boto3

# semaphore limit of 5, picked this number arbitrarily
maxthreads = 5
sema = threading.Semaphore(value=maxthreads)

logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('botocore').setLevel(logging.CRITICAL)
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

# Fetch the PD API token from PD_API_KEY_NAME key in SSM
PD_API_KEY = boto3.client('ssm').get_parameters(
        Names=[os.environ['PD_API_KEY_NAME']],
        WithDecryption=True)['Parameters'][0]['Value']

# Get the Current User on-call for a given schedule
def get_user(schedule_id):
    global PD_API_KEY
    headers = {
            'Accept': 'application/vnd.pagerduty+json;version=2',
            'Authorization': 'Token token={token}'.format(token=PD_API_KEY)
            }
    normal_schedule_url = 'https://api.pagerduty.com/schedules/{0}/users'.format(
        schedule_id
    )
    override_schedule_url = 'https://api.pagerduty.com/schedules/{0}/overrides'.format(
        schedule_id
    )
    # This value should be less than the running interval
    # It is best to use UTC for the datetime object
    t = datetime.now(timezone.utc) - timedelta(minutes=1)
    payload = {}
    payload['since'] = t.isoformat()
    payload['until'] = datetime.now(timezone.utc).isoformat()
    # If there is no override, then check the schedule directly
    override = requests.get(override_schedule_url, headers=headers, params=payload)
    if override.status_code == 404:
        logger.critical("ABORT: Not a valid schedule: {}".format(schedule_id))
        return False
    if override.json().get('overrides'): # is not []
        # TODO: This doesn't work with multiple overrides for the same minute
        username = override.json()['overrides'][0]['user']['summary'] + " (Override)"
    else:
        normal = requests.get(normal_schedule_url, headers=headers, params=payload)
        try:
            username = normal.json()['users'][0]['name']
        except IndexError:
            username = "No One :thisisfine:"
    logger.info("Currently on call: {}".format(username))
    return username

def get_pd_schedule_name(schedule_id):
    global PD_API_KEY
    headers = {
            'Accept': 'application/vnd.pagerduty+json;version=2',
            'Authorization': 'Token token={token}'.format(token=PD_API_KEY)
            }
    url = 'https://api.pagerduty.com/schedules/{0}'.format(
        schedule_id
    )
    r = requests.get(url, headers=headers)
    try:
        return r.json()['schedule']['name']
    except KeyError:
        logger.debug(r.status_code)
        logger.debug(r.json())
        return None

def get_slack_topic(channel):
    payload = {}
    payload['token'] = boto3.client('ssm').get_parameters(
        Names=[os.environ['SLACK_API_KEY_NAME']],
        WithDecryption=True)['Parameters'][0]['Value']
    payload['channel'] = channel
    r = requests.post('https://slack.com/api/channels.info', data=payload)
    try:
        current = r.json()['channel']['topic']['value']
        logger.debug("Current Topic: '{}'".format(current))
        return current
    except KeyError: # there is no topic
        return None

def update_slack_topic(channel, proposed_update):
    logger.debug("Entered update_slack_topic() with: {} {}".format(
        channel,
        proposed_update)
        )
    payload = {}
    payload['token'] = boto3.client('ssm').get_parameters(
        Names=[os.environ['SLACK_API_KEY_NAME']],
        WithDecryption=True)['Parameters'][0]['Value']
    payload['channel'] = channel

    # This is tricky to get correct for all the edge cases
    current_full_topic = get_slack_topic(channel)

    # This should match every case EXCEPT when onboarding a channel and it
    # already has a '|' in it. Workaround: Fix topic again and it will be
    # correct in the future
    current_full_topic_delimit_count = current_full_topic.count('|')
    c_delimit_count = current_full_topic_delimit_count - 1
    if c_delimit_count < 1: c_delimit_count = 1

    if current_full_topic: # is not None
        try:
            first_part = current_full_topic.rsplit('|', c_delimit_count)[0].strip()
            second_part = current_full_topic.replace(first_part + " |", "").strip()
        except IndexError: # if there is no '|' in the topic
            first_part = "none"
            second_part = current_full_topic
    else:
        first_part = "none"
        second_part = "." # if there is no topic, just add something

    if proposed_update != first_part:
        # slack limits topic to 250 chars
        topic =  "{} | {}".format(proposed_update, second_part)
        if len(topic) > 251:
            topic = topic[0:247] + "..."
        payload['topic'] = topic
        r = requests.post('https://slack.com/api/channels.setTopic', data=payload)
        return r.json()
    else:
        logger.info("Not updating slack, topic is the same")
        return None

def figure_out_schedule(s):
    # Purpose here is to find the schedule id if given a human readable name
    # fingers crossed that this regex holds for awhile. "PXXXXXX"
    if re.match('^P[a-zA-Z0-9]{6}', s):
        return s
    global PD_API_KEY
    headers = {
            'Accept': 'application/vnd.pagerduty+json;version=2',
            'Authorization': 'Token token={token}'.format(token=PD_API_KEY)
            }
    url = 'https://api.pagerduty.com/schedules/'
    payload = {}
    payload['query'] = s
    # If there is no override, then check the schedule directly
    r = requests.get(url, headers=headers, params=payload)
    try:
        # This is fragile. fuzzy search may not do what you want
        sid = r.json()['schedules'][0]['id']
    except IndexError:
        logger.debug("Schedule Not Found for: {}".format(s))
        sid = None
    return sid

def do_work(obj):
    # entrypoint of the thread
    sema.acquire()
    logger.debug("Operating on {}".format(obj))
    # schedule will ALWAYS be there, it is a ddb primarykey
    schedule = figure_out_schedule(obj['schedule']['S'])
    if schedule:
        username = get_user(schedule)
    else:
        logger.critical("Exiting: Schedule not found or not valid, see previous errors")
        return 127
    if username: # is not None, then it is valid and update the chat topic
        topic = "{} is on-call for '{}'".format(username, get_pd_schedule_name(schedule))
        if 'slack' in obj.keys():
            slack = obj['slack']['S']
            update_slack_topic(slack, topic)
        elif 'hipchat' in obj.keys():
            hipchat = obj['hipchat']['S']
            logger.critical("HipChat is not supported yet. Ignoring this entry...")
    sema.release()

def handler(event, context):
    print(event)
    ddb = boto3.client('dynamodb')
    response = ddb.scan(
                TableName=os.environ['CONFIG_TABLE'],
                )
    threads = []
    for i in response['Items']:
        thread = threading.Thread(target=do_work, args=(i,))
        threads.append(thread)
    [t.start() for t in threads]
    [t.join() for t in threads]

if __name__ == '__main__':
    get_user('P31BKVS')
