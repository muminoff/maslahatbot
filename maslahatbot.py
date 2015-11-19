# -*- coding: utf-8 -*-


import os
import time
import logging
import threading
import telegram
import requests
import redis
import datetime
import stathat

LAST_UPDATE_ID = None

subscription_start_text = """
Сиз Фейсбукдаги Маслаҳат.уз гуруҳи янгиликларига обуна бўлдингиз. Ушбу бот орқали гуруҳда чоп этилаётган постларни тез ва қулай тарзда билиб туришингиз мумкин.\n\nОбунани бекор қилиш учун /stop сўзини киритишингиз мумкин.
"""
subscription_stop_text = """
Сиз обунани бекор қилдингиз. Қайта обуна бўлиш учун /start сўзини киритинг.
"""
default_text = """
Ортиқча сўз ёзиш мумкин эмас. Обуна бўлиш учун /start, обунани бекор қилиш учун /stop сўзини киритинг.
"""

stat = stathat.StatHat()


def main():
    global LAST_UPDATE_ID
    telegram_token = os.environ.get("TELEGRAM_TOKEN")

    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger = logging.getLogger('Maslahat.uz')
    logger.setLevel(logging.DEBUG)

    # logger.debug("Initalizing bot ...")
    try:
        bot = telegram.Bot(telegram_token)
        # logger.debug("Connected to Telegram API")
    except telegram.error.TelegramError:
        pass
        # logger.warning("Cannot connect to Telegram server!")

    redis_url = os.environ.get("REDIS_URL")
    redis_conn = redis.from_url(redis_url)
    # logger.debug("Connected to Redis")

    # logger.debug("Receiving updates ...")
    try:
        LAST_UPDATE_ID = bot.getUpdates()[-1].update_id
        # logger.debug("Updates received")
    except IndexError:
        # logger.warning("No update received")
        LAST_UPDATE_ID = None

    # logger.debug("Starting heartbeat ...")
    heart_beat(logger, stat)
    # logger.debug("Waiting for updates ...")
    while True:
        bot_worker(redis_conn, bot, logger)
        check_facebook(redis_conn, bot, logger)
        check_announcements(redis_conn, bot, logger)


def heart_beat(logger, stat):
    threading.Timer(60.0, heart_beat, [logger, stat]).start()
    stat.ez_post_count('smuminov@gmail.com', 'bot_heartbeat', 1)
    # logger.debug('Heartbeat stat sent.')


def check_announcements(redis_conn, bot, logger):
    news_time_array = redis_conn.hkeys('news')

    for news_time in news_time_array:
        now_time = datetime.datetime.now().strftime('%Y%m%d%H')
        news_time = news_time.decode('utf-8')
        # logger.debug("Announcement found for %s", news_time)

        if news_time == now_time:
            # broadcast message
            # logger.debug("Broadcasting announcement ...")
            message = redis_conn.hget('news', news_time)
            # logger.debug(message.decode('utf-8'))

            chats = redis_conn.smembers('chats')
            for chat_id in chats:
                try:
                    bot.sendMessage(
                        chat_id=chat_id,
                        text=message.decode('utf-8'),
                        disable_web_page_preview=True)
                    # logger.debug("Sending announcement to %s", chat_id)
                except telegram.error.TelegramError:
                    # logger.warning("Sending failed to chat %s", chat_id)
                    pass

            redis_conn.hdel('news', news_time)
            # logger.debug("Announcement deleted")

    # else:
    #     logger.debug("No announcement for now")


def bot_worker(redis_conn, bot, logger):
    global LAST_UPDATE_ID

    start_time = time.time()
    telegram_updates = bot.getUpdates(offset=LAST_UPDATE_ID, timeout=10)
    end_time = time.time()
    time_taken = end_time - start_time
    # logger.debug("Got response from Telegram. Response time: {}".format(time_taken))
    stat.ez_post_count(
        'smuminov@gmail.com', 'telegram_response', time_taken)

    for update in telegram_updates:
        chat_id = update.message.chat_id
        message = update.message.text.encode('utf-8')

        command = message.decode('utf-8')
        if (command.startswith("/")):
            handle_command(redis_conn, bot, chat_id, command, logger)
            LAST_UPDATE_ID = update.update_id + 1

        else:
            logger.debug("Not recognized command \"%s\" from %s" % (
                command, chat_id))

            try:
                bot.sendMessage(
                    chat_id=chat_id,
                    text=default_text,
                    disable_web_page_preview=True)
            except telegram.error.TelegramError:
                logger.warning("Sending failed to chat %s", chat_id)
                pass
            LAST_UPDATE_ID = update.update_id + 1


def check_facebook(redis_conn, bot, logger):
    facebook_token = os.environ.get("FACEBOOK_TOKEN")
    start_time = time.time()
    resp = requests.get("https://graph.facebook.com/v2.5/1601597320127277/feed?access_token={TOKEN}&limit=10".format(TOKEN=facebook_token))
    end_time = time.time()
    time_taken = end_time - start_time
    # logger.debug("Got response from Facebook. Response time: {}".format(time_taken))
    stat.ez_post_count(
        'smuminov@gmail.com', 'facebook_response', time_taken)
    posts = resp.json()['data']
    post_text = "{text}\n\n{url}"

    for post in posts:

        if 'updated_time' in post:
            post_raw_time = post['updated_time'].split('T')
        elif 'created_time' in post:
            post_raw_time = post['created_time'].split('T')

        post_date = post_raw_time[0].replace('-', '')
        today_date = datetime.datetime.now().strftime('%Y%m%d')
        new_post_published = post_date == today_date

        post_id = post['id']
        if not new_post_published:
            continue

        if not redis_conn.sismember('posts', post_id) and 'message' in post:
            # logger.debug("New post %s published in Facebook", post_id)
            post_url = "https://fb.com/%s" % post_id
            data = post_text.format(
                text=post['message'].encode('utf8'), url=post_url)
            redis_conn.sadd('posts', post_id)
            logger.debug("Post ID %s inserted into DB", post_id)
            broadcast_subscribers(redis_conn, bot, post_id, data, logger)

            stat.ez_post_count('smuminov@gmail.com', 'post_created', 1)
            # logger.debug('Stat sent.')
    # else:
    #     logger.debug("No new post in Facebook")


def broadcast_subscribers(redis_conn, bot, post_id, data, logger):
    chats = redis_conn.smembers('chats')
    logger.debug("Broadcasting %s to subscriber ...", post_id)

    for chat_id in chats:
        logger.debug("Sending to chat %s", chat_id)
        try:
            bot.sendMessage(
                chat_id=chat_id,
                text=data,
                disable_web_page_preview=True)
            stat.ez_post_count('smuminov@gmail.com', 'post_delivered', 1)
            # logger.debug('Stat sent.')
        except telegram.error.TelegramError:
            logger.warning("Sending failed to chat %s", chat_id)
            redis_conn.srem('chats', chat_id)
            logger.warning("Removed Chat ID from DB %s", chat_id)
            pass


def handle_command(redis_conn, bot, chat_id, command, logger):

    if command == "/start":
        # logger.debug("Subscription start request from %s", chat_id)

        redis_conn.sadd('chats', chat_id)
        # logger.debug("Chat ID %d inserted", chat_id)

        stat.ez_post_count('smuminov@gmail.com', 'user_subscribed', 1)
        # logger.debug('Stat sent.')

        try:
            bot.sendMessage(
                chat_id=chat_id,
                text=subscription_start_text,
                disable_web_page_preview=True)
        except telegram.error.TelegramError:
            # logger.warning("Sending failed to chat %s", chat_id)
            pass

    elif command == "/stop":
        # logger.debug("Subscription stop request from %s", chat_id)

        redis_conn.srem('chats', chat_id)
        # logger.debug("Chat ID %d removed", chat_id)

        stat.ez_post_count('smuminov@gmail.com', 'user_unsubscribed', 1)
        # logger.debug('Stat sent.')

        try:
            bot.sendMessage(
                chat_id=chat_id,
                text=subscription_stop_text,
                disable_web_page_preview=True)
        except telegram.error.TelegramError:
            # logger.warning("Sending failed to chat %s", chat_id)
            pass

    else:
        # logger.debug("Not recognized command \"%s\" from %s" % (
            # command, chat_id))
        stat.ez_post_count('smuminov@gmail.com', 'user_sent_command', 1)
        # logger.debug('Stat sent.')

        try:
            bot.sendMessage(
                chat_id=chat_id,
                text=default_text,
                disable_web_page_preview=True)
        except telegram.error.TelegramError:
            # logger.warning("Sending failed to chat %s", chat_id)
            pass


if __name__ == '__main__':
    main()
