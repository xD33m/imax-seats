import os
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
from discord_webhook import DiscordWebhook
import sched
import time
import datetime
from hashlib import md5
import logging

# load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)

s = sched.scheduler(time.time, time.sleep)

# target URL
URL = os.getenv('TARGET_URL')

# your Discord webhook URL
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL')

# initial hash
current_hash = None 

def get_page_html(url):
    response = requests.get(url)
    return response.content, response.status_code

def check_day_body_change(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    
    # find the target div
    day_block = soup.find('strong', {'class': 'day-header day-so'}).parent
    day_body = day_block.find('span', {'class': 'day-body'}).text

    # create a new hash from the div content
    new_hash = md5(day_body.encode()).hexdigest()

    global current_hash

    # compare the new hash with the previous one
    if current_hash and new_hash != current_hash:
        current_hash = new_hash
        return True

    # update the hash
    current_hash = new_hash
    return False

def check_website(sc):
    now = datetime.datetime.now()
    page_html, status_code = get_page_html(URL)
    
    if status_code != 200:
        send_discord_alert('Failed to check the website content')

    if check_day_body_change(page_html):
        logging.info("Content has changed! (%s)", status_code)
        send_discord_alert('Website content has changed!')
    else:
        logging.info("No changes. (%s)", status_code)

    # check every 30 seconds
    s.enter(30, 1, check_website, (sc,))

def send_discord_alert(msg):
    now = datetime.datetime.now()
    msg_to_send = now.strftime("[%H:%M:%S] " + msg)
    webhook = DiscordWebhook(url=DISCORD_WEBHOOK_URL, content=msg_to_send)
    response = webhook.execute()

try:
    logging.info('IMAX Seat crawler started 🤖')
    send_discord_alert('IMAX Seat crawler started 🤖')
    s.enter(0, 1, check_website, (s,))
    s.run()
except Exception as e:
    logging.error('IMAX Seat crawler crashed: %s', str(e))
    send_discord_alert(f'IMAX Seat crawler crashed: {str(e)}')
    raise e