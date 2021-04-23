import argparse
import html
import json
import logging
import os
import pickle
import random
import re
import string
import sys
import traceback
import typing
from collections import OrderedDict
from configparser import ConfigParser
from datetime import datetime, timedelta
from threading import Timer
from urllib.request import urlopen
from urllib.error import HTTPError

import lmdb
from bs4 import BeautifulSoup
from dateutil.parser import parse
from telegram import (Chat, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto, ParseMode, ReplyKeyboardMarkup,
                      ReplyKeyboardRemove, Update)
from telegram.bot import Bot
from telegram.error import BadRequest, NetworkError
from telegram.ext import (BaseFilter, CallbackContext, CallbackQueryHandler,
                          CommandHandler, ConversationHandler, Filters,
                          MessageHandler, Handler, Updater)
from telegram.utils.helpers import DEFAULT_NONE


import time
from functools import wraps


def retry(ExceptionToCheck, tries=4, delay=3, backoff=2):
    """Retry calling the decorated function using an exponential backoff.

    http://www.saltycrane.com/blog/2009/11/trying-out-retry-decorator-python/
    original from: http://wiki.python.org/moin/PythonDecoratorLibrary#Retry

    :param ExceptionToCheck: the exception to check. may be a tuple of
        exceptions to check
    :type ExceptionToCheck: Exception or tuple
    :param tries: number of times to try (not retry) before giving up
    :type tries: int
    :param delay: initial delay between retries in seconds
    :type delay: int
    :param backoff: backoff multiplier e.g. value of 2 will double the delay
        each retry
    :type backoff: int
    :param logger: logger to use. If None, print
    :type logger: logging.Logger instance
    """
    def deco_retry(f):

        @wraps(f)
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 1:
                try:
                    return f(*args, **kwargs)
                except Exception as e:
                    if isinstance(e,ExceptionToCheck):
                        msg = "%s, Retrying in %d seconds..." % (str(e), mdelay)
                        logging.warning(msg)
                        time.sleep(mdelay)
                        mtries -= 1
                        mdelay *= backoff
                    else:
                        raise e
            return f(*args, **kwargs)

        return f_retry  # true decorator

    return deco_retry


class BotHandler:

    #All supported tags by telegram seprated by '|'
    # this program will handle images it self
    SUPPORTED_HTML_TAGS = '|'.join(('a','b','strong','i','em','code','pre','s','strike','del','u'))
    SUPPORTED_TAG_ATTRS = {'a':'href', 'img':'src', 'pre':'language'}

    STATE_ADD, STATE_EDIT, STATE_DELETE, STATE_CONFIRM = range(4)

    # --------------------[Decorators]--------------------

    def message_handler(self, filter_: BaseFilter):
        def decorator(func):
            self.dispatcher.add_handler(MessageHandler(filter_, func), group=1)
            return func
        return decorator

    def command(self, func = None, name = None):
        "add a command handler"
        def decorator(func = func):
            self.dispatcher.add_handler(CommandHandler((name if name else func.__name__), func), group=1)
            return func
        if not name:
            return(decorator(func))
        return decorator

    def adminCommand(self, func = None, skip_register = False):
        def decorator(func = func):
            def auth_and_run(u: Update, c: CallbackContext):
                if u.effective_user.id in self.adminID:
                    return func(u, c)
                else:
                    return self.__unknown__(u, c)
            if not skip_register:
                self.dispatcher.add_handler(
                    CommandHandler(func.__name__, auth_and_run), group=1)
            return auth_and_run

        if skip_register:
            return decorator
        return decorator()

    def ownerCommand(self, func = None, skip_register = False):
        def decorator(func = func):
            def auth_and_run(u: Update, c: CallbackContext):
                if u.effective_user.id == self.ownerID:
                    return func(u, c)
                else:
                    return self.__unknown__(u, c)
            if not skip_register:
                self.dispatcher.add_handler(
                    CommandHandler(func.__name__, auth_and_run), group=1)
            return auth_and_run

        if skip_register:
            return decorator
        return decorator()

    # ----------------------------------------------------
    # ----------------------[Init]------------------------

    def __init__(
        self,
        Token,
        source,
        env,
        chats_db,
        data_db,
        strings: dict,
        bug_reporter = None,
        debug = False):
        #----[USE SOCKES]----
        import socks
        s = socks.socksocket()
        s.set_proxy(socks.SOCKS5, "localhost", 9090)
        self.updater = Updater(Token, request_kwargs = {'proxy_url': 'socks5h://127.0.0.1:9090/'})
        #-----[NO PROXY]-----
        #self.updater = Updater(Token)
        #--------------------
        self.bot = self.updater.bot
        self.dispatcher = self.updater.dispatcher
        self.token = Token
        self.env = env
        self.chats_db = chats_db
        self.data_db = data_db
        self.adminID = self.__get_data__('adminID', [], DB = data_db)
        self.ownerID = self.__get_data__('ownerID', DB = data_db)
        self.admins_pendding = {}
        self.admin_token = []
        self.strings = strings
        self.source = source
        self.interval = self.__get_data__('interval', 5*60, data_db)
        self.__check__ = True
        self.bug_reporter = bug_reporter if bug_reporter else None
        self.debug = False

        if debug:
            def log_update(u: Update, c:CallbackContext):
                message = (
                    'Received a new update event from telegram\n'
                    f'update = {json.dumps(u.to_dict(), indent = 2, ensure_ascii = False)}\n'
                    f'user_data = {json.dumps(c.user_data, indent = 2, ensure_ascii = False)}\n'
                    f'chat_data = {json.dumps(c.chat_data, indent = 2, ensure_ascii = False)}'
                )
                logging.debug(message)
                if self.debug:
                    try:
                        self.bot.send_message(self.ownerID,html.escape(message), parse_mode = ParseMode.HTML)
                    except Exception as e:
                        self.log_bug(e,'Exception while sending update log to owner',ownerID=self.ownerID, message=html.escape(message))
            
            self.dispatcher.add_handler(MessageHandler(Filters.update,log_update))

            @self.ownerCommand
            def log_updates(u:Update, c:CallbackContext):
                self.debug = not self.debug
                if self.debug:
                    u.message.reply_text('Debug enabled. now bot sends all updates for you')
                else:
                    u.message.reply_text('Debug disabled.')

        @self.message_handler(Filters.update.edited_message)
        def handle_edited_msg(u: Update, c:CallbackContext):
            #TODO: Handle editing messages
            # Handle messages editing in /send_all could be usefull
            # labels: enhancement
            u.edited_message.reply_text(self.strings['edited-message'])

        @self.command
        def start(update: Update, _: CallbackContext):
            chat = update.effective_chat
            message = update.message
            user = update.effective_user
            data = chat.to_dict()
            data['members-count'] = chat.get_members_count()-1
            if chat.type == Chat.PRIVATE:
                data.update(user.to_dict())
                message.reply_markdown_v2(self.get_string('welcome'))
                if len(_.args) == 1:
                    if _.args[0] == self.token:
                        if user.id in self.adminID:
                            message.reply_text(
                                f'My dear {user.full_name}, I already know you as my lord!')
                        else:
                            message.reply_text(
                                f'Hi my dear {user.full_name}\nFrom now on, I know you as my lord\nyour id is: "{user.id}"')
                            self.adminID.append(user.id)
                            self.__set_data__(
                                'adminID', self.adminID, DB = data_db)

                            self.ownerID = user.id
                            self.__set_data__(
                                'ownerID', self.ownerID, DB = data_db)
                    elif _.args[0] in self.admin_token:
                        if user.id in self.adminID:
                            message.reply_text(
                                f'My dear {user.full_name}, I already know you as my admin!')
                        else:
                            message.reply_text(
                                'Owner must accept your request.\n⏳ please wait...')
                            self.admins_pendding[user.id] = _.args[0]
                            self.bot.send_message(
                                self.ownerID,
                                'Hi, A user wants to be admin:\n' +
                                f'tel-id:\t{user.id}\n' +
                                f'user-id:\t{user.username}\n' +
                                f'name:\t{user.full_name}',
                                reply_markup = InlineKeyboardMarkup(
                                    [[
                                        InlineKeyboardButton(
                                            '✅ Accept', callback_data = f'accept-{user.id}'),
                                        InlineKeyboardButton(
                                            '❌ Decline', callback_data = f'decline-{user.id}')
                                    ]])
                            )

            else:
                update.message.reply_markdown_v2(
                    self.get_string('group-intro'))

            self.__set_data__(key = str(chat.id), value = data)

        # --------------[owner commands]-----------------

        @self.ownerCommand
        def gentoken(u: Update, c: CallbackContext):
            if u.effective_user.id == self.ownerID:
                admin_token = ''.join(
                    [random.choice(string.ascii_letters+string.digits) for x in range(32)])
                self.admin_token.append(admin_token)
                u.message.reply_text(
                    admin_token
                )
            else:
                self.__unknown__(u, c)
                

        # --------------[admin commands]-----------------

        @self.adminCommand
        def my_level(u: Update, c: CallbackContext):
            if u.effective_user.id == self.ownerID:
                u.message.reply_text(
                    'Oh, my lord. I respect you.')
            elif u.effective_user.id in self.adminID:
                u.message.reply_text('Oh, my admin. Hi, How are you?')

        @self.adminCommand
        def state(u: Update, c: CallbackContext):
            members, chats = 0, 0
            msg = u.message.reply_text('⏳ Please wait, counting members...')
            with env.begin(self.chats_db) as txn:
                chats = int(txn.stat()["entries"])
                for key, value in txn.cursor():
                    v = pickle.loads(value)
                    members += v['members-count']
            msg.edit_text(
                f'👥chats:\t{chats}\n' +
                f'👤members:\t{members}\n' +
                f'🤵admins:\t{len(self.adminID)}'
                )

        @self.adminCommand
        def listchats(u: Update, c: CallbackContext):
            res = ''
            with env.begin(self.chats_db) as txn:
                res = 'total: '+str(txn.stat()["entries"])+'\n'
                for key, value in txn.cursor():
                    chat = pickle.loads(value)
                    if type(chat) is not type(dict()):
                        res+=html.escape(f'\n bad data type; type:{type(chat)}, value:{chat}\n')
                        continue
                    if 'username' in chat:
                        chat['username'] = '@'+chat['username']
                    res += html.escape(json.dumps(chat,
                                       indent = 2, ensure_ascii = False))
            u.message.reply_html(res)

        def add_keyboard (c:CallbackContext):
            keys = ['❌Cancel']
            if len(c.user_data['messages']):
                keys = ['✅Send', '👁Preview', '❌Cancel']
            markdown = c.user_data['parser'] == ParseMode.HTML
            return ReplyKeyboardMarkup(
                [
                    [('✅ HTML Enabled' if markdown else '◻️ HTML Disabled')],
                    keys
                ],
                resize_keyboard=True
            )

        @self.adminCommand(skip_register = True)
        def sendall(u: Update, c: CallbackContext):
            if u.effective_chat.type != Chat.PRIVATE:
                u.message.reply_text(
                    '❌ ERROR\nthis command only is available in private')
                return ConversationHandler.END
            c.user_data['last-message'] = u.message.reply_text('You can send text or photo.',disable_notification = True)
            c.user_data['messages'] = []
            c.user_data['parser'] = DEFAULT_NONE
            u.message.reply_text(
                'OK, Send a message to forward it to all users',
                reply_markup = add_keyboard(c))
            
            return self.STATE_ADD

        @self.adminCommand
        def send_feed_toall(u: Update, c: CallbackContext):
            self.send_feed(*self.read_feed(), self.get_string('last-feed'), self.iter_all_chats())

        @self.adminCommand
        def set_interval(u: Update, c: CallbackContext):
            if len(c.args) == 1:
                if c.args[0].isdigit():
                    self.interval = int(c.args[0])
                    self.__set_data__(
                        'interval', self.interval, self.data_db)
                    u.message.reply_text('✅ Interval changed to'+str(self.interval))
                    return
            u.message.reply_markdown_v2('❌ Bad command, use `/set_interval {new interval in seconds}`')

        # ----------------[User Commands]----------------

        @self.command
        def last_feed(u: Update, c: CallbackContext):
            if u.effective_user.id not in self.adminID and 'time' in c.user_data:
                if c.user_data['time'] > datetime.now():
                    u.message.reply_text(self.get_string('time-limit-error'))
                    return
            self.send_feed(*self.read_feed(),msg_header = self.get_string('last-feed'),chats = [(u.effective_chat.id, c.chat_data)])
            c.user_data['time'] = datetime.now() + timedelta(minutes = 2)      #The next request is available 2 minutes later

        @self.command(name='help')
        def _help(u: Update, c: CallbackContext):
            if u.effective_chat.id == self.ownerID:
                u.message.reply_text(self.get_string('owner-help'))
            if u.effective_chat.id in self.adminID:
                u.message.reply_text(self.get_string('admin-help'))
            u.message.reply_text(self.get_string('help'))

        @self.command
        def stop(u: Update, c: CallbackContext):
            logging.info(
                f'I had been removed from a chat. chat-id:{u.effective_chat.id}')
            with self.env.begin(self.chats_db, write = True) as txn:
                if txn.get(str(u.effective_chat.id).encode()):  # check exist
                    txn.delete(str(u.effective_chat.id).encode())

        # ----------[Conversation handlers]-------------

        def toggle_markdown(u: Update, c:CallbackContext):
            if c.user_data['parser'] == ParseMode.HTML:
                c.user_data['parser'] = DEFAULT_NONE
                u.message.reply_text('◻️ HTML Disabled', reply_markup=add_keyboard(c))
            else:
                c.user_data['parser'] = ParseMode.HTML
                u.message.reply_text('✅ HTML Enabled', reply_markup=add_keyboard(c))

        def add_text(u:Update, c:CallbackContext):
            text = u.message.text
            if c.user_data['parser'] == ParseMode.HTML:
                text = str(self.purge(text,False))
                    
            c.user_data['messages'].append(
                {
                    'type':'text',
                    'text': text,
                    'parser': c.user_data['parser']
                }
            )
            c.user_data['last-message'].delete()
            c.user_data['last-message'] = u.message.reply_text('OK, I received your message now what? (send a message to add)', 
            reply_markup = add_keyboard(c))
            return self.STATE_ADD

        def add_photo(u:Update, c:CallbackContext):
            text = u.message.caption
            if c.user_data['parser'] == ParseMode.HTML:
                text = str(self.purge(text,False))
            c.user_data['messages'].append(
                {
                    'type': 'photo',
                    'photo': u.message.photo[-1],
                    'caption': text,
                    'parser': c.user_data['parser']
                }
            )
            c.user_data['last-message'].delete()
            c.user_data['last-message'] = u.message.reply_text(
                'OK, I received photo%s now what? (send a message to add)'%('s' if len(u.message.photo)>1 else ''),
                reply_markup = add_keyboard(c))
            return self.STATE_ADD

        text_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton('✏️Edit', callback_data = 'edit'),
                InlineKeyboardButton('❌Delete', callback_data = 'delete')
            ]
        ])

        photo_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton('✏️Edit', callback_data = 'edit'),
                InlineKeyboardButton('📝Edit caption', callback_data = 'edit-cap'),
                InlineKeyboardButton('❌Delete', callback_data = 'delete')
            ]
        ])

        def cleanup_last_preview(chat_id, c: CallbackContext):
            if 'prev-dict' in c.user_data:
                for msg_id in c.user_data['prev-dict']:
                    c.bot.edit_message_reply_markup(
                        chat_id,
                        msg_id,
                    )

        def preview(u: Update, c: CallbackContext):
            cleanup_last_preview(u.effective_chat.id, c)
            c.user_data['prev-dict'] = dict()
            chat = u.effective_chat
            for msg in c.user_data['messages']:
                if msg['type'] == 'text':
                    try:
                        msg_id = chat.send_message(
                            msg['text'],
                            parse_mode = msg['parser'],
                            reply_markup = text_markup
                        ).message_id
                    except BadRequest as ex:
                        msg_id = chat.send_message(
                            msg['text']+'\n\n⚠️ CAN NOT PARSE.\n'+ex.message,
                            reply_markup = text_markup
                        ).message_id
                        c.user_data['had-error'] = True

                    c.user_data['prev-dict'][msg_id] = msg
                elif msg['type'] == 'photo':
                    try:
                        msg_id = chat.send_photo(
                            msg['photo'],
                            msg['caption'],
                            parse_mode = msg['parser'],
                            reply_markup = photo_markup
                        ).message_id
                    except BadRequest as ex:
                        msg_id = chat.send_photo(
                            msg['photo'],
                            caption = msg['caption']+'\n\n⚠️ CAN NOT PARSE.\n'+ex.message,
                            reply_markup = photo_markup
                        ).message_id
                        c.user_data['had-error'] = True

                    c.user_data['prev-dict'][msg_id] = msg
                else:
                    logging.error('UNKNOWN MSG TYPE FOUND\n'+str(msg))
                    c.bot.send_message(self.ownerID, 'UNKNOWN MSG TYPE FOUND\n'+str(msg))
                    self.bug_reporter.bug('unknown type message in preview', 'UNKNOWN MSG TYPE FOUND\n'+str(msg))

            if c.user_data.get('had-error'):
                c.user_data['last-message'] = u.message.reply_text(
                    '🛑 there is a problem with your messages, please fix them.',
                    reply_markup = add_keyboard(c))
            else:
                c.user_data['last-message'] = u.message.reply_text('OK, now what?  (send a message to add)',
                reply_markup = add_keyboard(c))
            return self.STATE_ADD

        def edit(u: Update, c: CallbackContext):
            query = u.callback_query
            edit_cap = query.data == 'edit-cap'
            query.answer()
            c.user_data['last-message'].delete()
            c.user_data['last-message'] = self.bot.send_message(u.effective_chat.id,
                '✏️ EDITING CAPTION\nSend new caption.' if edit_cap else '✏️ EDITING\nSend new edition.',
                reply_markup = ReplyKeyboardMarkup([['❌Cancel']],resize_keyboard = True))
            c.user_data['editing-prev-id'] = query.message.message_id
            c.user_data['edit-cap'] = edit_cap
            return self.STATE_EDIT

        def text_edited(u: Update, c:CallbackContext):
            if not u.message:
                return self.STATE_EDIT
            preview_msg_id = c.user_data['editing-prev-id']     #id of the message that bot sent as preview
            msg = c.user_data['prev-dict'][preview_msg_id]      #get msg by searching preview message id in prev-dict
            edited_txt = u.message.text
            if c.user_data['parser'] == ParseMode.HTML:
                edited_txt = str(self.purge(edited_txt, False))
            if msg.get('had-error'):
                del(msg['had-error'])
            if c.user_data.get('had-error'):
                del(c.user_data['had-error'])
            msg['parser'] = c.user_data['parser']
            if msg['type'] == 'text':
                msg['text'] = edited_txt            #change message text
                try:
                    c.bot.edit_message_text(        #edit preview message text
                        edited_txt,
                        u.effective_chat.id,
                        preview_msg_id,
                        parse_mode = msg['parser'],
                        reply_markup = text_markup
                    )
                except BadRequest as ex:
                    c.bot.edit_message_text(
                        edited_txt+'\n\n⚠️ CAN NOT PARSE.\n'+ex.message,
                        u.effective_chat.id,
                        preview_msg_id,
                        reply_markup = text_markup
                    )
                    msg['had-error'] = True
                    c.user_data['had-error'] = True
            elif msg['type'] == 'photo':
                if c.user_data['edit-cap']:
                    msg['caption'] = u.message.text
                    try:
                        c.bot.edit_message_caption(
                            u.effective_chat.id,
                            preview_msg_id,
                            caption = edited_txt,
                            parse_mode = msg['parser'],
                            reply_markup = photo_markup
                        )
                    except BadRequest as ex:
                        c.bot.edit_message_caption(
                            u.effective_chat.id,
                            preview_msg_id,
                            caption = edited_txt+'\n\n⚠️ CAN NOT PARSE.\n'+ex.message,
                            reply_markup = photo_markup
                        )
                        msg['had-error'] = True
                        c.user_data['had-error'] = True
                else:
                    #change message type from photo to text
                    msg['type'] = 'text'
                    del(msg['photo'], msg['caption'])
                    msg['text'] = edited_txt
                    c.bot.edit_message_caption(
                        caption = '⚠️ This message type had been changed from photo to text. '+\
                        'You can request for a new preview to see this message.',
                        chat_id = u.effective_chat.id,
                        message_id = preview_msg_id
                    )
            else:
                #Log this bug
                logging.error('UNKNOWN MSG TYPE FOUND\n'+str(msg))
                c.bot.send_message(self.ownerID, 'UNKNOWN MSG TYPE FOUND\n'+str(msg))

            if c.user_data.get('had-error'):
                c.user_data['last-message'] = u.message.reply_text(
                    '🛑 there is a problem with your messages, please fix them.',
                    parse_mode = ParseMode.MARKDOWN_V2,
                reply_markup = add_keyboard(c))
            else:
                c.user_data['last-message'] = u.message.reply_text('✅ Message edited; now you can add more messages or send it',
                reply_markup = add_keyboard(c))
            return self.STATE_ADD

        def photo_edited(u: Update, c: CallbackContext):
            preview_msg_id = c.user_data['editing-prev-id']                             #id of the message that bot sent as preview
            msg = c.user_data['prev-dict'][preview_msg_id]                              #get msg by searching preview message id in prev-dict
            if msg.get('had-error'):
                del(msg['had-error'])
            if c.user_data.get('had-error'):
                del(c.user_data['had-error'])
            msg['parser'] = c.user_data['parser']
            msg['photo'] = u.message.photo[-1]
            msg['caption'] = u.message.caption
            if c.user_data['parser'] == ParseMode.HTML:
                msg['caption'] = str(self.purge(msg['caption'],False))
            if c.user_data['parser'] == ParseMode.MARKDOWN_V2:
                msg['caption'] = u.message.caption_markdown_v2
                
            if msg['type'] == 'photo':
                try:
                    c.bot.edit_message_media(
                        u.effective_chat.id,
                        preview_msg_id,
                        media = InputMediaPhoto(
                            media = msg['photo'],
                            caption = msg['caption'],
                            parse_mode = msg['parser'])
                    )
                except BadRequest as ex:
                    c.bot.edit_message_media(
                        u.effective_chat.id,
                        preview_msg_id,
                        media = InputMediaPhoto(
                            media = msg['photo'],
                            caption = msg['caption']+'\n\n⚠️ CAN NOT PARSE.\n'+ex.message)
                    )
                    msg['had-error'] = True
                    c.user_data['had-error'] = True
            elif msg['type'] == 'text':
                #change message type to photo
                msg['type'] = 'photo'
                del(msg['text'])
                c.bot.edit_message_text(
                    '⚠️ This message type had been changed from text to photo. '+\
                    'You can request for a new preview to see this message.',
                    u.effective_chat.id,
                    preview_msg_id,
                )
            else:
                #report bug
                logging.error('UNKNOWN MSG TYPE FOUND\n'+str(msg))
                c.bot.send_message(self.ownerID, 'UNKNOWN MSG TYPE FOUND\n'+str(msg))

            if c.user_data.get('had-error'):
                c.user_data['last-message'] = u.message.reply_text(
                    '🛑 there is a problem with your messages, please fix them.',
                    parse_mode = ParseMode.MARKDOWN_V2,
                reply_markup = add_keyboard(c))
            else:
                c.user_data['last-message'] = u.message.reply_text('✅ Message edited; now you can add more messages or send it',
                reply_markup = add_keyboard(c))
            return self.STATE_ADD

        def delete(u: Update, c: CallbackContext):
            query = u.callback_query
            query.answer('✅ Deleted')
            preview_msg_id = query.message.message_id
            msg = c.user_data['prev-dict'][preview_msg_id]
            c.user_data['messages'].remove(msg)
            del(c.user_data['prev-dict'][preview_msg_id])
            query.edit_message_text('❌')
            c.user_data['last-message'].delete()
            c.user_data['last-message'] = self.bot.send_message(
                u.effective_chat.id, 'OK, now you can send message to add', reply_markup = add_keyboard(c))
            return self.STATE_ADD

        def deleting(u: Update, c: CallbackContext):
            query = u.callback_query
            query.answer()
            query.edit_message_reply_markup(
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        '🛑 Are you sure?', callback_data = 'None')],
                    [InlineKeyboardButton('🔴 Yes', callback_data = 'yes'), InlineKeyboardButton(
                        '🟢 No', callback_data = 'no')]
                ])
            )
            c.user_data['last-message'].delete()
            c.user_data['last-message'] = self.bot.send_message(
                u.effective_chat.id, '⏳ Deleting a message...', reply_markup = ReplyKeyboardRemove())
            return self.STATE_DELETE

        def confirm(u: Update, c: CallbackContext):
            c.user_data['last-message'].delete()
            c.user_data['last-message'] = self.bot.send_message(u.effective_chat.id,
                'Are you sure, you want to send message' +
                ('s' if len(c.user_data['messages']) > 1 else '') +
                'to all users, groups and channels?',
                reply_markup = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("👍Yes, that's OK!", callback_data = 'yes'),
                            InlineKeyboardButton("✋No, stop!", callback_data = 'no')
                        ]
                    ]
                ))
            return self.STATE_CONFIRM

        def send(u: Update, c: CallbackContext):
            query = u.callback_query
            if c.user_data.get('had-error'):
                query.answer()
                u.effective_chat.send_message(
                    '🛑 there is a problem with your messages, please fix them.',
                    parse_mode = ParseMode.MARKDOWN_V2,
                    reply_markup = add_keyboard(c)
                )
                return self.STATE_ADD
            query.answer(
                '✅ Done\nSending message to all users, groups and channels', show_alert = True)
            logging.info('Sending message to chats')
            c.user_data['last-message'].delete()
            c.user_data['last-message'] = self.bot.send_message(u.effective_chat.id,
            '✅ Done\nSending message to all users, groups and channels')

            def send_message(chat_id):
                chat = c.bot.get_chat(chat_id)
                for msg in c.user_data['messages']:
                    #send message to admin for a debug!
                    if msg['type'] == 'text':
                        try:
                            chat.send_message(
                                msg['text'],
                                parse_mode = msg['parser']
                            ).message_id
                        except BadRequest as ex:
                            chat.send_message(
                                msg['text']+'\n\n⚠️ CAN NOT PARSE.\n'+ex.message,
                                reply_markup = text_markup
                            )
                            c.user_data['had-error'] = True
                            msg['had-error'] = True
                            return self.STATE_ADD
                    elif msg['type'] == 'photo':
                        try:
                            chat.send_photo(
                                msg['photo'],
                                msg['caption'],
                                parse_mode = msg['parser']
                            ).message_id
                        except BadRequest as ex:
                            chat.send_photo(
                                msg['photo'],
                                caption = msg['caption']+'\n\n⚠️ CAN NOT PARSE.\n'+ex.message
                            ).message_id
                            c.user_data['had-error'] = True
                            msg['had-error'] = True
                            return self.STATE_ADD

            res = send_message(u.effective_chat.id)
            if res:
                u.effective_chat.send_message(
                    '🛑 there is a problem with your messages, please fix them.',
                    parse_mode = ParseMode.MARKDOWN_V2,
                    reply_markup = add_keyboard(c)
                )
                return res

            for chat_id, chat_data in self.iter_all_chats():
                if chat_id != u.effective_chat.id:
                    try:
                        send_message(chat_id)
                    except Exception as e:
                        self.log_bug(e,'exception while trying to send message to a chat', chat_id = chat_id, chat_data = chat_data)
            
            cleanup_last_preview(u.effective_chat.id, c)
            for key in ('messages', 'prev-dict', 'had-error', 'edit-cap', 'editing-prev-id'):
                if key in c.user_data:
                    del(c.user_data[key])
            return ConversationHandler.END

        def confirm_admin(u: Update, c: CallbackContext):
            query = u.callback_query
            if u.effective_user.id == self.ownerID:
                new_admin_id = int(query.data[7:])
                self.bot.send_message(
                    new_admin_id,
                    f'✅ Accepted, From now on, I know you as my admin')
                self.adminID.append(new_admin_id)
                self.__set_data__('adminID', self.adminID, DB = data_db)
                self.admin_token.remove(self.admins_pendding[new_admin_id])
                del(self.admins_pendding[new_admin_id])
                query.answer('✅ Accepted')
                query.message.edit_text(query.message.text+'\n\n✅ Accepted')
            else:
                query.answer()

        def decline_admin(u: Update, c: CallbackContext):
            query = u.callback_query
            if u.effective_user.id == self.ownerID:
                new_admin_id = int(query.data[8:])
                self.bot.send_message(
                    new_admin_id,
                    f"❌ Declined, Owner didn't accepted your request")
                self.admin_token.remove(self.admins_pendding[new_admin_id])
                del(self.admins_pendding[new_admin_id])
                query.answer('❌ Declined')
                query.message.edit_text(query.message.text+'\n\n❌ Declined')
            else:
                query.answer()

        def unknown_query(u: Update, c: CallbackContext):
            query = u.callback_query
            logging.warning('unknown query, query data:'+query.data)
            query.answer("❌ ERROR\nUnknown answer", show_alert = True,)

        def cancel(state) -> typing.Callable:
            _cancel = None
            if state in (self.STATE_ADD, self.STATE_CONFIRM):
                def _cancel(u: Update, c: CallbackContext):
                    for key in ('messages', 'prev-dict', 'had-error', 'edit-cap', 'editing-prev-id'):
                        if key in c.user_data:
                            del(c.user_data[key])

                    c.user_data['last-message'].delete()
                    c.user_data['last-message'] = self.bot.send_message(u.effective_chat.id,
                        'Canceled', reply_markup = ReplyKeyboardRemove())
                    return ConversationHandler.END
            elif state == self.STATE_EDIT:
                def _cancel(u: Update, c: CallbackContext):
                    for key in ('edit-cap', 'editing-prev-id'):
                        if key in c.user_data:
                            del(c.user_data[key])
                    return self.STATE_ADD
            elif state == self.STATE_DELETE:
                def _cancel(u: Update, c: CallbackContext):
                    query = u.callback_query
                    query.answer('❌ Canceled')
                    query.edit_message_reply_markup(
                        InlineKeyboardMarkup([
                            [InlineKeyboardButton('✏️Edit', callback_data = 'edit'), InlineKeyboardButton(
                                '❌Delete', callback_data = 'delete')]
                        ]))
                    c.user_data['last-message'].delete()
                    c.user_data['last-message'] = self.bot.send_message(
                        u.effective_chat.id, 
                        'OK, now what?  (send a message to add)',
                        reply_markup = add_keyboard(c))
                    return self.STATE_ADD
            return _cancel

        send_all_conv_handler = ConversationHandler(
            entry_points = [CommandHandler('sendall', sendall)],
            states = {
                self.STATE_ADD: [
                    MessageHandler(Filters.regex("^✅Send$"), confirm),
                    MessageHandler(Filters.regex("^👁Preview$"), preview),
                    MessageHandler(Filters.regex(
                        "^❌Cancel$"), cancel(self.STATE_ADD)),
                    MessageHandler(Filters.regex("^✅ HTML Enabled$")|Filters.regex("^◻️ HTML Disabled$"), toggle_markdown),
                    MessageHandler(Filters.text, add_text),
                    MessageHandler(Filters.photo, add_photo)
                ],
                self.STATE_EDIT: [
                    MessageHandler(Filters.regex(
                        "^❌Cancel$"), cancel(self.STATE_EDIT)),
                    MessageHandler(Filters.regex("^✅ HTML Enabled$")|Filters.regex("^◻️ HTML Disabled$"), toggle_markdown),
                    MessageHandler(Filters.text, text_edited),
                    MessageHandler(Filters.photo, photo_edited)
                ],
                self.STATE_DELETE: [
                    CallbackQueryHandler(cancel(self.STATE_DELETE), pattern = '^no$'),
                    CallbackQueryHandler(delete, pattern = '^yes$'),
                ],
                self.STATE_CONFIRM: [
                    CallbackQueryHandler(send, pattern = '^yes$'),
                    CallbackQueryHandler(cancel(self.STATE_CONFIRM), pattern = '^no$')
                ]
            },
            fallbacks = [
                CallbackQueryHandler(edit, pattern = '^edit(-cap)?$'),
                CallbackQueryHandler(deleting, pattern = '^delete$'),
                CallbackQueryHandler(unknown_query, pattern = '.*')
            ],
            per_user = True
        )
        self.dispatcher.add_handler(send_all_conv_handler, group=1)
        self.dispatcher.add_handler(CallbackQueryHandler(
            confirm_admin, pattern = 'accept-.*'), group=1)
        self.dispatcher.add_handler(CallbackQueryHandler(
            decline_admin, pattern = 'decline-.*'), group=1)

        def onjoin(u: Update, c: CallbackContext):
            for member in u.message.new_chat_members:
                if member.username == self.bot.username:
                    data = u.effective_chat.to_dict()
                    data['members-count'] = u.effective_chat.get_members_count()-1
                    self.__set_data__(key = str(u.effective_chat.id), value = data)
                    self.bot.send_message(
                        self.ownerID,
                        '<i>Joined to a chat:</i>\n' +
                            html.escape(json.dumps(
                                data, indent = 2, ensure_ascii = False)),
                        ParseMode.HTML,
                        disable_notification = True)
                    if u.effective_chat.type != Chat.CHANNEL:
                        u.message.reply_markdown_v2(
                            self.get_string('group-intro'))

        def onkick(u: Update, c: CallbackContext):
            if u.message.left_chat_member['username'] == self.bot.username:
                data = self.__get_data__(str(u.effective_chat.id))
                if data:
                    self.bot.send_message(
                        self.ownerID,
                        '<i>Kicked from a chat:</i>\n' +
                            html.escape(json.dumps(
                                data, indent = 2, ensure_ascii = False)),
                        ParseMode.HTML,
                        disable_notification = True)
                    with self.env.begin(self.chats_db, write = True) as txn:
                        txn.delete(str(u.effective_chat.id).encode())

        self.dispatcher.add_handler(MessageHandler(
            Filters.status_update.new_chat_members, onjoin), group=1)
        self.dispatcher.add_handler(MessageHandler(
            Filters.status_update.left_chat_member, onkick), group=1)

        @self.message_handler(Filters.command)
        def unknown(u: Update, c: CallbackContext):
            u.message.reply_text(self.get_string('unknown'))

        self.__unknown__ = unknown

        # unknown commands and messages handler (register as last handler)
        @self.message_handler(Filters.all)
        def unknown_msg(u: Update, c: CallbackContext):
            u.message.reply_text(self.get_string('unknown-msg'))

        def error_handler(update: object, context: CallbackContext) -> None:
            """Log the error and send a telegram message to notify the developer."""

            self.log_bug(
                context.error,
                'Exception while handling an update',
                not isinstance(context.error, NetworkError),
                update = update.to_dict() if isinstance(update, Update) else str(update),
                user_data = context.user_data,
                chat_data = context.chat_data
            )

        self.dispatcher.add_error_handler(error_handler)

    # ----------------------------------------------------

    def log_bug(self, ex:Exception, msg='', report = True, disable_notification = False,**args):
        tb = ex.__traceback__
        tb_list = traceback.format_exception(None, ex, tb)
        tb_string = ''.join(tb_list)
        s = traceback.extract_tb(tb)
        f = s[-1]
        lineno = f.lineno
        filename = os.path.basename(f.filename)
        exception_type = type(ex).__name__

        if self.bug_reporter and report:
            self.bug_reporter.bug(f'L{lineno}@{filename}: {exception_type}', f'{msg}\n{tb_string}', line=lineno, file=filename)
        
        message = (
            '<b>An exception was raised</b>\n'
            f'L{lineno}@{filename}: {exception_type}\n'
            f'{html.escape(msg)}\n'
            f'<pre>{tb_string}</pre>'
        )
        if len(args):
            message+='\n\nExtra info:'
            msg+='\n\nExtra info'
            for key, value in args.items():
                message+=f'\n<pre>{key} = {html.escape(json.dumps(value, indent = 2, ensure_ascii = False))}</pre>'
                msg+=f'\n{key} = {json.dumps(value, indent = 2, ensure_ascii = False)}'
        
        logging.exception(msg, exc_info=ex)
        try:
            self.bot.send_message(chat_id = self.ownerID, text = message, parse_mode = ParseMode.HTML, disable_notification = disable_notification)
        except:
            logging.exception('can not send message to owner')

    def purge(self, html_str:str, images=True):
        tags = self.SUPPORTED_HTML_TAGS
        if images:
            tags+='|img'
        purge = re.compile(r'</?(?!(?:%s)\b)\w+[^>]*/?>'%tags).sub      #This regex will purge any unsupported tag
        soup = BeautifulSoup(purge('', html_str), 'html.parser')
        for tag in soup.descendants:
            #Remove any unsupported attribute
            if tag.name in self.SUPPORTED_TAG_ATTRS:
                attr = self.SUPPORTED_TAG_ATTRS[tag.name]
                if attr in tag.attrs:
                    tag.attrs = {attr: tag[attr]}
            else:
                tag.attrs = dict()
        return soup

    @retry(HTTPError,10)
    def get_feed(self):
        with urlopen(self.source) as f:
            return f.read().decode('utf-8')

    def read_feed(self):
        feeds_xml = None
        try:
            feeds_xml = self.get_feed()
        except Exception as e:
            self.log_bug(e,'exception while trying to get last feed', False, True)
            return None, None
        
        soup_page = BeautifulSoup(feeds_xml, 'xml')
        feeds_list = soup_page.findAll("item")
        skip = re.compile(r'</?[^>]*name = "skip"[^>]*>').match               #This regex will search for a tag named as "skip" like: <any name = "skip">
        for feed in feeds_list:
            try:
                description = str(feed.description.text)
                if not skip(description):     #if regex found something skip this post
                    soup = self.purge(description)
                    description = str(soup)
                    messages = [{'type': 'text', 'text': description, 'markup': None}]
                    #Handle images
                    images = soup.find_all('img')
                    first = True
                    if images:
                        for img in images:
                            sep = str(img)
                            have_link = img.parent.name == 'a'
                            if have_link:
                                sep = str(img.parent)
                            first_part, description = description.split(sep, 1)
                            if first:   #for first message
                                if first_part != '':
                                    messages[0] = {'type': 'text', 'text': first_part, 'markup': None}
                                    msg = {'type': 'image',
                                    'src': img['src'], 'markup': None}
                                    if have_link:
                                        msg['markup'] = [[InlineKeyboardButton('Open image link', img.parent['href'])]]
                                    messages.append(msg)
                                else:
                                    msg = {'type': 'image', 'src': img['src'], 'markup': None}
                                    if have_link:
                                        msg['markup'] = [[InlineKeyboardButton('Open image link', img.parent['href'])]]
                                    messages[0] = msg
                                first = False
                            else:
                                messages[-1]['text'] = first_part
                                msg = {'type': 'image',
                                    'src': img['src'], 'markup': None}
                                if have_link:
                                    msg['markup'] = [[InlineKeyboardButton('Open image link', img.parent['href'])]]
                                messages.append(msg)
                        #End for img
                        messages[-1]['text'] = description
                    #End if images
                    return feed, messages
            except Exception as e:
                self.log_bug(e,'Exception while reading feed', feed = str(feed))
                return None, None

    def send_feed(self, feed, messages, msg_header, chats):
        #TODO:Un-handled users block
        # it seems that a user had blocked bot and bot tried to send him message.
        # labels: bug
        if len(messages) != 0:
            try:
                if messages[-1]['markup']:
                    messages[-1]['markup'].append(
                        [InlineKeyboardButton('View post', str(feed.link.text))])
                else:
                    messages[-1]['markup'] = [[InlineKeyboardButton('View post', str(feed.link.text))]]
                
                msg_header = '<i>%s</i>\n\n<b><a href = "%s">%s</a></b>\n' % (
                    msg_header, feed.link.text, feed.title.text)
                messages[0]['text'] = msg_header+messages[0]['text']
                for chat_id, chat_data in chats:
                    for msg in messages:
                        try:
                            if msg['type'] == 'text':
                                self.bot.send_message(
                                    chat_id,
                                    msg['text'],
                                    parse_mode = ParseMode.HTML,
                                    reply_markup = InlineKeyboardMarkup(msg['markup']) if msg['markup'] else None
                                )
                            elif msg['type'] == 'image':
                                if msg['text'] == '':
                                    msg['text'] = None
                                self.bot.send_photo(
                                    chat_id,
                                    msg['src'],
                                    msg['text'],
                                    parse_mode = ParseMode.HTML,
                                    reply_markup = InlineKeyboardMarkup(msg['markup']) if msg['markup'] else None
                                )
                        except Exception as e:
                            self.log_bug(e, 'Exception while sending a feed to a user', message = msg, chat_id = chat_id, chat_data = chat_data)
                            break
            except Exception as e:
                self.log_bug(e,'Exception while trying to send feed', messages = messages)

    def iter_all_chats(self):
        with env.begin(self.chats_db) as txn:
            for key, value in txn.cursor():
                yield key.decode(), pickle.loads(value)

    def check_new_feed(self):
        feed, messages = self.read_feed()
        if feed:
            date = self.__get_data__('last-feed-date', DB = self.data_db)
            if date:
                feed_date = parse(feed.pubDate.text)
                if feed_date > date:
                    self.__set_data__('last-feed-date',
                                      feed_date, DB = self.data_db)
                    self.send_feed(feed, messages, self.get_string('new-feed'), self.iter_all_chats())
            else:
                feed_date = parse(feed.pubDate.text)
                self.__set_data__('last-feed-date',
                                  feed_date, DB = self.data_db)
                self.send_feed(feed, messages, self.get_string('new-feed'), self.iter_all_chats())
        if self.__check__:
            self.check_thread = Timer(self.interval, self.check_new_feed)
            self.check_thread.start()


    def __get_data__(self, key, default = None, DB = None, do = lambda data: pickle.loads(data)):
        DB = DB if DB else self.chats_db
        data = None
        with self.env.begin(DB) as txn:
            data = txn.get(key.encode(), default)
        if data is not default and callable(do):
            return do(data)
        else:
            return data

    def __set_data__(self, key, value, over_write = True, DB = None, do = lambda data: pickle.dumps(data)):
        DB = DB if DB else self.chats_db
        if not callable(do):
            do = lambda data: data
        with self.env.begin(DB, write = True) as txn:
            return txn.put(key.encode(), do(value), overwrite = over_write)

    def get_string(self, string_name):
        return ''.join(self.strings[string_name])

    def run(self):
        self.updater.start_polling()
        # check for new feed
        self.check_new_feed()

    def idle(self):
        self.updater.idle()
        self.updater.stop()
        self.__check__ = False
        self.check_thread.cancel()
        if self.check_thread.is_alive():
            self.check_thread.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser('main.py',
        description='Open source Telegram RSS-Bot server by bsimjoo\n'+\
            'https://github.com/bsimjoo/Telegram-RSS-Bot'
        )
    
    parser.add_argument('-r','--reset',
    help='Reset stored data about chats or bot data',
    default=False,required=False,choices=('data','chats','all'))

    parser.add_argument('-c','--config',
    help='Specify config file',
    default='user-config.conf', required=False, type=argparse.FileType('r'))

    args = parser.parse_args(sys.argv[1:])
    config = ConfigParser(allow_no_value=False)
    with args.config as cf:
        config.read_string(cf.read())
    main_config = config['main']
    file_name = main_config.get('log-file')
    logging.basicConfig(
        format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        filename=file_name,
        level = logging._nameToLevel.get(main_config.get('log-level','INFO').upper(),logging.INFO))
    env = lmdb.open(main_config.get('db-path','db.lmdb'), max_dbs = 3)
    chats_db = env.open_db(b'chats')
    data_db = env.open_db(b'config')        #using old name for compatibility

    if args.reset:
        answer = input(f'Are you sure you want to Reset all "{args.reset}"?(yes | anything else means no)')
        if answer != 'yes':
            exit()
        else:
            if args.reset in ('data', 'all'):
                with env.begin(data_db, write=True) as txn:
                    d=env.open_db()
                    txn.drop(d)
            if args.reset in ('chats', 'all'):
                with env.begin(chats_db, write=True) as txn:
                    d=env.open_db()
                    txn.drop(d)

    language = main_config.get('language','en-us')
    strings_file = main_config.get('strings-file', 'Default-strings.json')
    checks=[
        (strings_file, language),
        (strings_file, 'en-us'),
        ('Default-strings.json', language),
        ('Default-strings.json', 'en-us')
    ]
    strings = None
    for file, language in checks:
        if os.path.exists(file):
            with open(file) as f:
                strings = json.load(f)
            if language in strings:
                strings = strings[language]
                logging.info(f'using "{language}" language from "{file}" file')
                break
            else:
                logging.error(f'"{language}" language code not found in "{file}"')
        else:
            logging.error(f'file "{file}" not found')

    if not strings or strings == dict():
        logging.error('Cannot use a strings file. exiting...')
        exit(1)

    bug_reporter = None
    reporter = None
    http_reporter = False

    if main_config.get('bug-reporter', 'off') in ('online', 'offline'):
        import BugReporter
        bugs_file = main_config.get('bugs-file','bugs.json')
        use_git = main_config.getboolean('use-git',False)
        git = main_config.get('git-command','git')
        git_source = main_config.get('git-source')
        bug_reporter = BugReporter.BugReporter(bugs_file, use_git, git, git_source)
        
        if main_config.get('bug-reporter') == 'online':
            try:
                import cherrypy  # user can ignore installing this module just if doesn't need reporting on http
            
                class root:

                    @cherrypy.expose
                    def index(self):
                        res = '''<html>
                        <head>
                        <style>
                            html, body{
                                background-color: #1b2631;
                                color:  #d6eaf8;
                            }
                            pre, ssh-pre{
                                width:80%;
                                max-height: 30%;
                                margin: auto;
                                background-color: #873600;
                                color:  white;
                                border-radius: 10px;
                                padding: 10px;
                                overflow-x: auto;
                                white-space: pre-wrap;
                                word-wrap: break-word;
                            }
                            a:visited{
                                color: #a569bd
                            }
                        </style>
                        </head>
                        <body><h1 style="background-color: #d6eaf8; border-radius:10px; color:black">🐞 Bugs</h1>
                        <p><b>What is this?</b> This project uses a simple web
                        server to report bugs (exceptions) in a running application.</p>
                        <h6><a href="/json">Show raw JSON</a></h6>'''

                        if bug_reporter.bugs_count:
                            res+=f'<h2>😔 {bug_reporter.bugs_count} bug(s) found</h2>'
                            for tag, content in bug_reporter.bugs.items():
                                link = ''
                                if 'file' in content and bug_reporter.use_git:
                                    lineno = content['line']
                                    filename = content['file']
                                    if os.path.exists(filename):
                                        link = f' <a href="{bug_reporter.git_source}/blob/{bug_reporter.commit}/{filename}#L{lineno}">🔸may be here: L{lineno}@{filename}</a></h3>'
                                res+=f'<h3>&bull;Tag: <kbd>"{tag}"</kbd> Count: {content["count"]} {link}</h3>'
                                if content["message"]:
                                    res+=f'<pre>{content["message"]}</pre>'
                        else:
                            res+='<h1 align="center">😃 NO 🐞 FOUND 😉</h1>'

                        res+='</body></html>'
                        return res

                    @cherrypy.expose
                    @cherrypy.tools.json_out()
                    def json(self):
                        return bug_reporter.data
                
                conf = main_config.get('reporter-config-file','Bug-reporter.conf')
                if os.path.exists(conf):
                    cherrypy.log.access_log.propagate = False
                    cherrypy.tree.mount(root(),'/')
                    cherrypy.config.update(conf)
                    cherrypy.engine.start()
                    http_reporter = True
            except ModuleNotFoundError:
                logging.error('Cherrypy module not found, please first make sure that it is installed and then use http-bug-reporter')
                logging.info('Can not run http bug reporter, skipping http, saving bugs in bugs.json')
            except Exception as Argument:
                logging.exception("Error occurred while running http server")
                logging.info('Can not run http bug reporter, skipping http, saving bugs in bugs.json')
            else:
                logging.info(f'reporting bugs with http server and saving them as bugs.json')
        else:
            logging.info(f'saving bugs in bugs.json')

    token = main_config.get('token')
    if not token:
        logging.error("No Token, exiting")
        sys.exit()

    debug = main_config.getboolean('debug',fallback=False)

    bot_handler = BotHandler(token, main_config.get('source','https://pcworms.blog.ir/rss/'), env,
                             chats_db, data_db, strings, bug_reporter, debug)
    bot_handler.run()
    bot_handler.idle()
    if bug_reporter:
        logging.info('saving bugs report')
        bug_reporter.dump()
    if http_reporter:
        logging.info('stoping http reporter')
        cherrypy.engine.stop()
    env.close()
    
