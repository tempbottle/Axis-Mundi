import threading
import Queue
import time
from datetime import datetime, timedelta
#import paho.mqtt.client as mqtt
#from paho.mqtt.client import MQTT_ERR_SUCCESS, mqtt_cs_connected
from mqtt_client import MQTT_ERR_SUCCESS, mqtt_cs_connected, MQTTMessage
from mqtt_client import Client as mqtt
from storage import Storage
import gnupg
from messaging import Message, Messaging, Contact
from utilities import queue_task, current_time, got_pgpkey, parse_user_name, full_name, Listing, get_age, json_from_clearsigned
import json
import socks
import socket
from calendar import timegm
from time import gmtime
import random
import re
from collections import defaultdict
from pprint import pprint
import string
from platform import system as get_os
import textwrap
from constants import *
import pybitcointools as btc
from btc_utils import *


class messaging_loop(threading.Thread):
    # Authentication shall set the password to a PGP clear-signed message containing the follow data only
    # broker-hostname:pgpkeyid:UTCdatetime
    # The date time shall not contain seconds and will be chekced on the
    # server to be no more than +/- 5 minutes fromcurrent server time

    def __init__(self, pgpkeyid, pgppassphrase, dbpassphrase, database, homedir, appdir, q, q_res, workoffline=False, looking_glass=False):
        print "NOTICE: Backend Thread Init"
        self.targetbroker = None
        self.mypgpkeyid = pgpkeyid
        self.q = q  # queue for client (incoming queue_task requests)
        self.q_res = q_res  # results queue for client (outgoing)
        self.database = database
        self.homedir = homedir
        if get_os() == 'Windows':
            self.pgpdir = homedir + '/application data/gnupg'
        else:
            self.pgpdir = homedir + '/.gnupg'
        self.appdir = appdir
        self.test_mode = False
        self.dbsecretkey = dbpassphrase
        self.onion_brokers = []
        self.i2p_brokers = []
        self.clearnet_brokers = []
        # TODO - account for filenames with spaces on windows
        self.gpg = gnupg.GPG(gnupghome=self.pgpdir, options={ '--primary-keyring=' + self.appdir + '/pubkeys.gpg',
                                                              '--no-emit-version', '--keyserver=hkp://127.0.0.1:5000',
                                                              '--keyserver-options=auto-key-retrieve=yes,http-proxy='
                                                             })
        self.client = mqtt()
        self.pgp_passphrase = pgppassphrase
        self.profile_text = ''
        self.display_name = None
        self.myMessaging = Messaging(
            self.mypgpkeyid, self.pgp_passphrase, self.pgpdir, appdir)
        self.sub_inbox = str("user/" + self.mypgpkeyid + "/inbox")
        self.pub_profile = str("user/" + self.mypgpkeyid + "/profile")
        self.pub_key = str("user/" + self.mypgpkeyid + "/key")
        self.pub_items = str("user/" + self.mypgpkeyid + "/items")
        self.pub_directory = str("user/" + self.mypgpkeyid + "/directory")
        self.storageDB = Storage
        self.connected = False
        self.workoffline = workoffline
        self.looking_glass = looking_glass
        self.shutdown = False
        self.message_retention = 30  # default 30 day retention of messages before auto-purge
        self.allow_unsigned = True  # allow unsigned PMs by default
        # This will hold the state of any outstanding message tasks
        self.task_state_messages = defaultdict(defaultdict)
        # This will hold the state of any outstanding key retrieval tasks
        self.task_state_pgpkeys = defaultdict(defaultdict)
        # This will hold cached listing items, entries will be created as
        # needed
#        self.memcache_listing_items = defaultdict(defaultdict)
        self.publish_identity = True
        self.btc_master_key = ''
        # Published Roles
        self.is_seller = False
        self.is_notary = False # if true once dbconfig is read then notary screens and functions will be available
        self.is_arbiter = False # if true once dbconfig is read then arbiter screens and functions will be available
        # Non-published roles (these roles must not be exposed to Axis Mundi)
        self.is_broker_admin = False # if true once dbconfig is read then broker admin screens and functions will be available

        threading.Thread.__init__(self)

    # TODO: Check that these parameters are right, had to add "flags" which
    # may break "rc"
    def on_connect(self, client, userdata, flags, rc):
        print "Connected"
        self.connected = True
        flash_msg = queue_task(0, 'flash_status', 'On-line')
        self.q_res.put(flash_msg)
        flash_msg = queue_task(
            0, 'flash_message', 'Connected to broker ' + self.targetbroker)
        self.q_res.put(flash_msg)
        self.setup_message_queues(client)
        # TODO: Call function to process any queued outbound messages(pm &
        # transaction) - github issue #5

    def on_disconnect(self, client, userdata, rc):
        print "MQTT on_disconnect..."
        self.connected = False
        flash_msg = queue_task(0, 'flash_status', 'Off-line')
        self.q_res.put(flash_msg)
        if self.shutdown:
            flash_msg = queue_task(
                0, 'flash_message', 'Disconnected from ' + self.targetbroker)
        else:
            # We were disconnected - try another broker
            self.select_random_broker()
            flash_msg = queue_task(
                0, 'flash_message', 'Broker unavailable, trying another broker: ' + self.targetbroker)
            self.q_res.put(flash_msg)
            try:
                #client._password = self.make_pgp_auth() # original token only valid for 3 minutes
                print "Attempting to connect to another broker: " + self.targetbroker
                self.client.username_pw_set(self.mypgpkeyid, self.make_pgp_auth())
                self.client.connect(self.targetbroker, 1883, 30)
                #self.connected = True
                #flash_msg = queue_task(0, 'flash_status', 'On-line')
                #self.q_res.put(flash_msg)
            except:
                print "Reconnection failure"
                self.connected = False
                flash_msg = queue_task(0, 'flash_status', 'Off-line')
                self.q_res.put(flash_msg)
                flash_msg = queue_task(
                    0, 'flash_message', 'Reconnection failed, giving up for now. Check your Tor/i2p connection')
                self.q_res.put(flash_msg)

    # TODO - make use of onpublish and onsubscribe callbacks instead of
    # checking status of call to publish or subscribe
    def on_publish(self, client, userdata, mid):
        print "On publish for " + str(mid) + " " + str(userdata)

    def on_subscribe(self, client, userdata, mid, granted_qos):
        print "On subscribe for " + str(mid) + " " + str(userdata)

    def on_message(self, client, userdata, msg):
        # Key blocks and directory entries are a special case because they are
        # not signed so we check for them first
        if re.match('user\/[A-F0-9]{16}\/key', msg.topic):
            # Here is a key, store it and unsubscribe
            print "Key Retrieved from " + str(msg.topic)
            client.unsubscribe(msg.topic)
            # TODO: Check we have really been sent a PGP key block and check if
            # it really is the same as the topic key
            keyid = msg.topic[msg.topic.index('/') + 1:msg.topic.rindex('/')]
            try:
                state = self.task_state_pgpkeys[keyid]['state']
            except KeyError:
                state = None
            if state == KEY_LOOKUP_STATE_REQUESTED:
                session = self.storageDB.DBSession()
                cachedkey = self.storageDB.cachePGPKeys(key_id=keyid,
                                                        updated=datetime.strptime(
                                                            current_time(), "%Y-%m-%d %H:%M:%S"),
                                                        keyblock=msg.payload)
                session.add(cachedkey)
                session.commit()
                self.task_state_pgpkeys[keyid][
                    'state'] = KEY_LOOKUP_STATE_FOUND
                print "Retrieved key committed"
            else:
                print "Dropping unexpected pgp key received for keyid: " + keyid
                # TODO: Shall we just always allow keys to be received?
            return None
# imp_res = self.gpg.import_keys(msg.payload) # we could import it here
# but we use the local keyserver
        elif re.match('user\/[A-F0-9]{16}\/directory', msg.topic):
            # Here is a directory entry, store it and unsubscribe
            if not self.looking_glass:
                client.unsubscribe(msg.topic)
            keyid = msg.topic[msg.topic.index('/') + 1:msg.topic.rindex('/')]
            print "Directory entry: " + msg.payload + " " + msg.topic
            # TODO: Use a memcache rather than write direct to database, use a periodic task to save memcache to cacheDirectory table (every 30 seconds or so)
            # TODO: This memory cache will be necessary to deal with a large
            # number of users in the directory
            print "Adding directory entry "
            try:
                display_dict = json.loads(msg.payload) # This is one of the few unsigned messages (the oother being /key (the pgp public key itself))
                display_name = display_dict['display_name'] # This must always be present even if empty
            except:
                print "Unable to decode directory message from " + keyid
                return
#            print display_dict['is_seller']
            try:
                is_seller = display_dict['is_seller'] # Separate try block for pre 0.1 clients TODO: remove and place these in the preceeding try block
                is_notary = display_dict['is_notary'] # Separate try block for pre 0.1 clients TODO: remove and place these in the preceeding try block
                is_arbiter = display_dict['is_arbiter'] # Separate try block for pre 0.1 clients TODO: remove and place these in the preceeding try block
                is_looking_glass = display_dict['is_looking_glass'] # Separate try block for pre 0.1 clients TODO: remove and place these in the preceeding try block

            except:
                print "Error reading directory flags for " + keyid
                is_seller = False
                is_notary = False
                is_arbiter = False
                is_looking_glass = False
            session = self.storageDB.DBSession()
            # If it already exists then update
            if session.query(self.storageDB.cacheDirectory).filter(self.storageDB.cacheDirectory.key_id == keyid).count() > 0:
                direntry = session.query(self.storageDB.cacheDirectory).filter(self.storageDB.cacheDirectory.key_id == keyid).update({
                    self.storageDB.cacheDirectory.updated: datetime.strptime(current_time(), "%Y-%m-%d %H:%M:%S"),
                    self.storageDB.cacheDirectory.display_name: display_name,
                    self.storageDB.cacheDirectory.is_notary: is_notary,
                    self.storageDB.cacheDirectory.is_arbiter: is_arbiter,
                    self.storageDB.cacheDirectory.is_seller: is_seller,
                    self.storageDB.cacheDirectory.is_looking_glass: is_looking_glass
                })
            else:
                direntry = self.storageDB.cacheDirectory(key_id=keyid,
                                                         updated=datetime.strptime(
                                                             current_time(), "%Y-%m-%d %H:%M:%S"),
                                                         display_name=display_name,
                                                         is_notary=is_notary,
                                                         is_arbiter=is_arbiter,
                                                         is_seller=is_seller,
                                                         is_looking_glass=is_looking_glass)
                session.add(direntry)
            session.commit()
            return
        # let us see if we can parse the base message and have the necessary
        # public key to check signature, if any
        incoming_message = self.myMessaging.GetMessage(
            msg.payload, self, msg.topic, allow_unsigned=self.allow_unsigned)
        # If this message was deferred lets stack it...
        if incoming_message == False:
            print "Message received but not processed correctly so dropped..."
            return
        elif incoming_message == MSG_STATE_KEY_REQUESTED:
            # getmessage should have already updated task_state_pgpkeys and
            # task_state_messages
            print "Incoming message has been deferred while signing key is requested"
            return

        if msg.topic == self.sub_inbox and (self.allow_unsigned or incoming_message.signed):
            message = incoming_message
            if message == False:
                print "Message was invalid"
            elif message.type == 'Private Message':
                # Calculate purge date for this message
                purgedate = datetime.now() + timedelta(days=self.message_retention)
                flash_msg = queue_task(
                    0, 'flash_message', 'Private message received from ' + message.sender)
                self.q_res.put(flash_msg)
                session = self.storageDB.DBSession()
                new_db_message = self.storageDB.PrivateMessaging(
                    sender_key=message.sender,
                    recipient_key=message.recipient,
                    message_id=message.id,
                    message_purge_date=purgedate,
                    message_date=datetime.strptime(
                        current_time(), "%Y-%m-%d %H:%M:%S"),
                    subject=message.subject,
                    body=message.body,
                    message_sent=False,
                    message_read=False,
                    message_direction="In"
                )
                session.add(new_db_message)
                session.commit()
            elif message.type == 'Order Message':
                # message.sub_messages[0] contains the outermost pgp signed message block
                # verifiy signature, record pgp key and json load result - store key and order message in stage[0]
                # while order message.raw_item exists
                         #verifiy signature, record pgp key and json load result in stage[n]- store key and order message
                # read stage array backwards to get contract chain
                # if chain checks out then update order db
#                print message.sub_messages[0]
                order_stages = []
                current_stage_signed = message.sub_messages[0] # process the outermost clearsigned message
                #current_stage_signed = re.sub('(?m)^- ',"",current_stage_signed) # pgp wont do this for us

                current_stage_sig_check = self.gpg.verify(current_stage_signed)
#                print "current_stage_signed"
#                print current_stage_signed
                while current_stage_sig_check.fingerprint: #  using fingerprint means we must already have the pgpkey which we will if received the order message over the network
                                                        # TODO get pgp key first if needed (like with messages) so we can process orders out of band (like email)
                    current_stage_verified = json_from_clearsigned(current_stage_signed)
                    try:
                        order_stage = (json.loads(current_stage_verified))
                    except:
                        print "Error unable to decode json order stage message " + current_stage_verified
                        return False
                    order_stages.append(order_stage)
                    # see if there is a parent block
                    try:
                        current_stage_signed = str(order_stage['parent_contract_block'])#.replace('\\n','\n') # process the next most outermost clearsigned message
                    except KeyError:
                        # This will happen once we reach the top of the chain
                        # or it could be some invalid crap to be dropped
                        current_stage_signed = ''
#                    current_stage_signed = re.sub('(?m)^- ',"",current_stage_signed) # pgp wont do this for us
                    print "Verifying nested signature"
                    print current_stage_signed
                    current_stage_sig_check = self.gpg.verify(current_stage_signed)
                order_stages.reverse()
                print "Order message contains a contract chain of " + str(len(order_stages)) + " blocks"
                for stage in order_stages:
                    print "-------------------------------------------------------------------------------"
                    try:
                        print 'Order Stage: ' + stage['order_status'] +' '+ str(stage)
                    except:
                        print 'Item: ' + str(stage)
                #status = order_stages['order_status']# order_stages[0]['order_status']
                flash_msg = queue_task(
                    0, 'flash_message', 'Order message received from ' + message.sender)
                self.q_res.put(flash_msg)

                # TODO : Process incoming order message - is it a new order or an update to an existing order
                session = self.storageDB.DBSession()
  #              if session.query(self.storageDB.Orders).filter(self.storageDB.Orders.key_id == keyid).count() > 0:
  #                  direntry = session.query(self.storageDB.cacheDirectory).filter(self.storageDB.cacheDirectory.key_id == keyid).update({
  #                      self.storageDB.cacheDirectory.updated: datetime.strptime(current_time(), "%Y-%m-%d %H:%M:%S"),
  #                      self.storageDB.cacheDirectory.display_name: display_dict['display_name']
  #                  })
  #              else:
#                new_db_message = self.storageDB.PrivateMessaging(
#                                                                    sender_key=message.sender,
#                                                                    recipient_key=message.recipient,
#                                                                    message_id=message.id,
#                                                                    message_purge_date=datetime.strptime(current_time(),"%Y-%m-%d %H:%M:%S"),
#                                                                    message_date=datetime.strptime(current_time(),"%Y-%m-%d %H:%M:%S"),
#                                                                    subject=message.subject,
#                                                                    body=message.body,
#                                                                    message_sent=False,
#                                                                    message_read=False,
#                                                                    message_direction="In"
#                                                                 )
#                session.add(new_db_message)
#                session.commit()
        elif re.match('user\/[A-F0-9]{16}\/profile', msg.topic) and incoming_message.signed:
            # Here is a profile, store it and unsubscribe
            if not self.looking_glass:
                client.unsubscribe(msg.topic)
            keyid = msg.topic[msg.topic.index('/') + 1:msg.topic.rindex('/')]
            # TODO: Check we have really been sent a valid profile message for the key indicated
            # print msg.payload
            # self.myMessaging.GetMessage(msg.payload,self,allow_unsigned=False)
            # # Never allow unsigned profiles
            profile_message = incoming_message
            if profile_message:
                if not keyid == profile_message.sender:
                    print "Profile was signed by a different key - discarding..."
                else:
                    profile_text = profile_message.sub_messages['profile']
                    session = self.storageDB.DBSession()

                    if 'display_name' in profile_message.sub_messages and 'profile' in profile_message.sub_messages and 'avatar_image' in profile_message.sub_messages:
                        # If it already exists then update
                        if session.query(self.storageDB.cacheProfiles).filter(self.storageDB.cacheProfiles.key_id == keyid).count() > 0:
                            cachedprofile = session.query(self.storageDB.cacheProfiles).filter(self.storageDB.cacheProfiles.key_id == keyid).update({
                                self.storageDB.cacheProfiles.updated: datetime.strptime(current_time(), "%Y-%m-%d %H:%M:%S"),
                                self.storageDB.cacheProfiles.display_name: profile_message.sub_messages['display_name'],
                                self.storageDB.cacheProfiles.profile_text: profile_message.sub_messages['profile'],
                                self.storageDB.cacheProfiles.avatar_base64: profile_message.sub_messages['avatar_image']
                            })
                        else:
                            cachedprofile = self.storageDB.cacheProfiles(key_id=keyid,
                                                                         updated=datetime.strptime(
                                                                             current_time(), "%Y-%m-%d %H:%M:%S"),
                                                                         display_name=profile_message.sub_messages[
                                                                             'display_name'],
                                                                         profile_text=profile_message.sub_messages[
                                                                             'profile'],
                                                                         avatar_base64=profile_message.sub_messages['avatar_image'])
                            session.add(cachedprofile)
                        session.commit()

                        session.commit()
                    else:
                        print "Profile message did not contain mandatory fields"
        # imp_res = self.gpg.import_keys(msg.payload) # we could import it here
        # but we use the local keyserver
            else:
                print "No profile message found in profile returned from " + keyid
        elif re.match('user\/[A-F0-9]{16}\/items', msg.topic) and incoming_message.signed:
            # Here is a listings message, store it and unsubscribe
            if not self.looking_glass:
                client.unsubscribe(msg.topic)
            keyid = msg.topic[msg.topic.index('/') + 1:msg.topic.rindex('/')]
            # print msg.payload
            # self.myMessaging.GetMessage(msg.payload,self,allow_unsigned=False)
            # # Never allow unsigned listings
            listings_message = incoming_message
            if listings_message:
                #                print listings_message
                if not keyid == listings_message.sender:
                    print "Listings were signed by a different key - discarding..."
                else:
                    if listings_message.sub_messages:
                        listings = listings_message.sub_messages.__str__()
                    else:
                        # user has no listings
                        print "User " + keyid + ' has no listings'
                        listings = None
                    print "Listings message received from " + keyid
                    # add this to the cachelistings table
                    session = self.storageDB.DBSession()
                    if listings_message.type == 'Listings Message':
                        # If it already exists then update
                        if session.query(self.storageDB.cacheListings).filter(self.storageDB.cacheListings.key_id == keyid).count() > 0:
                            print "There appears to be an existing listings message in the db cache for user " + keyid
                            cachedlistings = session.query(self.storageDB.cacheListings).filter(self.storageDB.cacheListings.key_id == keyid).update({
                                self.storageDB.cacheListings.updated: datetime.strptime(current_time(), "%Y-%m-%d %H:%M:%S"),
                                self.storageDB.cacheListings.listings_block: listings
                            })
                        else:
                            print "There is nothing in the listings cache for user, creating new cache entry"
                            cachedlistings = self.storageDB.cacheListings(key_id=keyid,
                                                                          updated=datetime.strptime(
                                                                              current_time(), "%Y-%m-%d %H:%M:%S"),
                                                                          listings_block=listings)
                            session.add(cachedlistings)
                        session.commit()

                        print "Trying to extract items from valid listings message..."
                        if listings:
                            verified_listings = self.get_items_from_listings(
                                keyid, listings_message.sub_messages)
                            # Purge any existing entries from table
                            session.query(self.storageDB.cacheItems).filter(self.storageDB.cacheItems.key_id == keyid).delete()
                            for verified_listing in verified_listings:
                                cacheditem = self.storageDB.cacheItems(id=verified_listing['id'], key_id=keyid,
                                                                              updated=datetime.strptime(current_time(), "%Y-%m-%d %H:%M:%S"),
                                                                              listings_block=verified_listing['raw_contract'],
                                                                              title=verified_listing['item'],
                                                                              category=verified_listing['category'],
                                                                              description=verified_listing['description'],
                                                                              qty_available = verified_listing['qty'],
                                                                              order_max_qty = verified_listing['max_order_qty'],
                                                                              price = verified_listing['unit_price'],
                                                                              currency_code = verified_listing['currency'],
                                                                              shipping_options = json.dumps(verified_listing['shipping_options']),
                                                                              image_base64 = verified_listing['image'],
                                                                              seller_btc_stealth= verified_listing['stealth_address'],
                                                                              publish_date = datetime.strptime(verified_listing['publish_date'],"%Y-%m-%d %H:%M:%S"))

                                session.add(cacheditem)
                            session.commit()
                        else:
                            print "No items message found in listings returned from " + keyid
                            # TODO - should we remove existing items from the cache? some items may have been directly imported (sent from seller via email or pm)
                    else:
                        print "Dropping incoming message because it is not a Listings Message"

        else:
            if incoming_message.signed:
                flash_msg = queue_task(
                    0, 'flash_error', 'Message received for non-inbox topic - ' + msg.topic)
                print "Unsigned message recv: " + msg.payload
            else:
                flash_msg = queue_task(
                    0, 'flash_error', 'Unsigned message received for topic - ' + msg.topic)
                print "Non PM message recv: " + msg.payload
            self.q_res.put(flash_msg)

    def setup_message_queues(self, client):
        # Sub to directory each conenction for now
        # TODO ONLY PUB IF WE NEED TO, check our topics with subs first and
        # only publish if there is a difference with the local db copy
        # TODO: We definitely don't want to do this each time user connects -
        # put such SUBs in an one time client SUB setup
        client.subscribe('user/+/directory', 1)
        if self.looking_glass:
            client.subscribe('user/+/items', 1)
            client.subscribe('user/+/profile', 1)
        # Our generic incoming queue, qos=1
        client.subscribe(self.sub_inbox, 1)
        # Our published pgp key block, qos=1, durable
        client.publish(self.pub_key, self.gpg.export_keys(
            self.mypgpkeyid, False, minimal=True),  1, True)
        # Calculate stealth address from the first child key of the master key
        stealth_address = create_stealth_address(btc.privkey_to_pubkey(
            btc.bip32_extract_key(btc.bip32_ckd(self.btc_master_key, 1))))
        # temporary buying keys will be generated from the second child of the master key

        # build and send profile message
        profile_message = Message()
        profile_message.type = 'Profile Message'
        profile_message.sender = self.mypgpkeyid
        profile_dict = {}
        profile_dict['display_name'] = self.display_name
        profile_dict['profile'] = self.profile_text
        profile_dict['avatar_image'] = self.avatar_image
        profile_dict['stealth_address'] = stealth_address
        # todo: use .append to add a submessage instead
        profile_message.sub_messages = profile_dict
        profile_out_message = Messaging.PrepareMessage(
            self.myMessaging, profile_message)
        # Our published profile queue, qos=1, durable
        client.publish(self.pub_profile, profile_out_message, 1, True)
        # build and send listings message
        listings_out_message = Messaging.PrepareMessage(
            self.myMessaging, self.create_listings_msg())
        # Our published items queue, qos=1, durable
        client.publish(self.pub_items, listings_out_message, 1, True)
        # build and send directory entry if user selected to publish
        if self.publish_identity:
            #           directory_message = Message()
            #            directory_message.type = 'Directory Message'
            #            directory_message.sender = self.mypgpkeyid
            directory_dict = {}
            directory_dict['display_name'] = self.display_name
            directory_dict['is_seller'] = self.is_seller
            directory_dict['is_notary'] = self.is_notary
            directory_dict['is_arbiter'] = self.is_arbiter
            directory_dict['is_looking_glass'] = self.looking_glass
            str_directory = json.dumps(directory_dict)
            print "Directory string to publish : " + str_directory
            # Our published items queue, qos=1, durable
            client.publish(self.pub_directory, str_directory, 1, True)

    def create_listings_msg(self):
        listings_message = Message()
        listings_message.type = 'Listings Message'
        listings_message.sender = self.mypgpkeyid
        # Calculate stealth address from the first child key of the master key
        stealth_address = create_stealth_address(btc.privkey_to_pubkey(
            btc.bip32_extract_key(btc.bip32_ckd(self.btc_master_key, 1))))
        session = self.storageDB.DBSession()
        # We're only going to publish listings marked as public=True  #
        # .filter_by(public=True)
        listings = session.query(
            self.storageDB.Listings).filter_by(public=True).all()
        if not listings:
            listings_message.body = "User is not publishing any items"
            listings_message.sub_messages = None
        else:  # We have listings, iterate and add a sub-message per listing

            for listing_item in listings:
                listings_dict = {}
                listings_dict['id'] = listing_item.id
                listings_dict['item'] = listing_item.title
                listings_dict['category'] = listing_item.category
                listings_dict['description'] = listing_item.description
                listings_dict['image'] = listing_item.image_base64
                listings_dict['unit_price'] = listing_item.price
                listings_dict['currency'] = listing_item.currency_code
                listings_dict['qty'] = listing_item.qty_available
                listings_dict['max_order_qty'] = listing_item.order_max_qty
                listings_dict['publish_date'] = current_time()
                listings_dict['stealth_address'] = stealth_address
                listings_dict['shipping_options'] = listing_item.shipping_options
                # listings_dict_str = json.dumps(listings_dict)
                listings_dict_str = json.dumps(listings_dict, sort_keys=True)
                listings_dict_str = textwrap.fill(
                    listings_dict_str, 80, drop_whitespace=False)
                signed_item = str(self.gpg.sign(
                    listings_dict_str, keyid=self.mypgpkeyid, passphrase=self.pgp_passphrase))
                listings_message.sub_messages.append(
                    signed_item)  # listings_dict_str)
                # listings_dict.clear()
        return listings_message

    def sendMessage(self, client, message):
        message.recipient = parse_user_name(message.recipient).pgpkey_id
        # do we have this key?
        if not got_pgpkey(self.storageDB, message.recipient):
            # need to get the recipients public key
            self.task_state_messages[message.id]['state'] = MSG_STATE_NEEDS_KEY
            self.task_state_messages[message.id]['message'] = message
            # return False # Message processing is deferred pending a key, added
            # message to task_state_messages todo remove line to saveto db even if no
            # key is present
        else:
            outMessage = self.myMessaging.PrepareMessage(message)
            if not outMessage:
                flash_msg = queue_task(
                    0, 'flash_error', 'Error: Message could not be prepared')
                self.q_res.put(flash_msg)
                return False
            res = client.publish(
                "user/" + message.recipient + "/inbox", outMessage, 1, False)
            if res[0] == MQTT_ERR_SUCCESS:
                flash_msg = queue_task(
                    0, 'flash_message', 'Message sent to ' + message.recipient)
                message.sent = True
                self.q_res.put(flash_msg)
            else:
                flash_msg = queue_task(
                    0, 'flash_message', 'Message queued for ' + message.recipient)
                self.q_res.put(flash_msg)
                self.task_state_messages[message.id][
                    'state'] = MSG_STATE_QUEUED
                self.task_state_messages[message.id]['message'] = message
        # Calculate purge date for this message
        purgedate = datetime.now() + timedelta(days=self.message_retention)
        if message.datetime_sent:
            formatted_msg_sent_date = datetime.strptime(
                message.datetime_sent, "%Y-%m-%d %H:%M:%S")
        else:
            formatted_msg_sent_date = None
        # Write the message to the database as long as we havent already -
        # check to see if message id already exists first
        session = self.storageDB.DBSession()
        if message.type == "Order Message":  # This is an outbound order message, update db order
            order = session.query(self.storageDB.Orders).filter(self.storageDB.Orders.orderid==message.subject).filter(self.storageDB.Orders.is_synced == False).first()
            print "Sent order id " + message.subject
            order.is_synced = True
            session.commit()

        elif message.type == "Private Message":
            # If it already exists then update
            if session.query(self.storageDB.PrivateMessaging).filter(self.storageDB.PrivateMessaging.message_id == message.id).count() > 0:
                # Message ID already esists in db. in theory all we could be
                # doing now is updating sent status and sent date
                message_to_update = session.query(self.storageDB.PrivateMessaging).filter(self.storageDB.PrivateMessaging.message_id == message.id).update({
                    self.storageDB.PrivateMessaging.message_date: formatted_msg_sent_date,
                    self.storageDB.PrivateMessaging.message_sent: message.sent
                })
            else:
                new_db_message = self.storageDB.PrivateMessaging(
                    sender_key=message.sender,
                    sender_name=message.sender_name,
                    recipient_key=message.recipient,
                    recipient_name=message.recipient_name,
                    message_id=message.id,
                    message_purge_date=purgedate,
                    message_date=formatted_msg_sent_date,
                    subject=message.subject,
                    body=message.body,
                    message_sent=message.sent,
                    message_read=message.read,
                    message_direction="Out"
                )
                session.add(new_db_message)
        session.commit()

    def delete_pm(self, id):
        flash_msg = queue_task(0, 'flash_message', 'Deleted private message ')
        self.q_res.put(flash_msg)
        # Write the message to the database
        session = self.storageDB.DBSession()
        session.query(self.storageDB.PrivateMessaging).filter_by(
            id=id).delete()
        session.commit()

    def make_pgp_auth(self):
        password_message = {}
        password_message['time'] = str(timegm(gmtime()) / 60)
        password_message['broker'] = self.targetbroker
        password_message['key'] = self.gpg.export_keys(
            self.mypgpkeyid, False, minimal=False)
        password = str(self.gpg.sign(json.dumps(password_message),
                                     keyid=self.mypgpkeyid, passphrase=self.pgp_passphrase))
        return password

    def price_to_btc(self,price,currency):
        session = self.storageDB.DBSession()
        currency_rec = session.query(self.storageDB.currencies).filter_by(code=currency).first()
        rate = currency_rec.exchange_rate
        btc_val = str(float(rate) * float(price))
        return (btc_val)

    def select_random_broker(self):
        transports = []
        if self.i2p_proxy_enabled and len(self.i2p_brokers) > 0:
            transports.append('i2p')
        if self.proxy_enabled and len(self.onion_brokers) > 0:
            transports.append('tor')
        transport = random.choice(transports)
        if transport == 'i2p':
            self.targetbroker = random.choice(self.i2p_brokers)
        elif transport == 'tor':
            self.targetbroker = random.choice(self.onion_brokers)

    def new_contact(self, contact):
        if contact.pgpkey != "":
            importedkey = self.gpg.import_keys(contact.pgpkey)
            contact.pgpkeyid = str(importedkey.fingerprints[0][-16:])
            if not importedkey.count == 1 and contact.pgpkeyid:
                flash_msg = queue_task(
                    0, 'flash_error', 'Contact not added: unable to extract PGP key ID for ' + contact.displayname)
                self.q_res.put(flash_msg)
                return False
        elif not contact.pgpkeyid:
            return False
        flash_msg = queue_task(0, 'flash_message', 'Added contact ' +
                               contact.displayname + '(' + contact.pgpkeyid + ')')
        self.q_res.put(flash_msg)
        session = self.storageDB.DBSession()
        new_db_contact = self.storageDB.Contacts(
            contact_name=contact.displayname,
            contact_key=contact.pgpkeyid,
            #
        )
        session.add(new_db_contact)
        cachedkey = self.storageDB.cachePGPKeys(key_id=contact.pgpkeyid,
                                                updated=datetime.strptime(
                                                    current_time(), "%Y-%m-%d %H:%M:%S"),
                                                keyblock=contact.pgpkey)
        session.add(cachedkey)
        session.commit()

    def new_listing(self, listing):
        if listing.title != "":
            pass
        else:
            return False
        flash_msg = queue_task(
            0, 'flash_message', 'Added listing ' + listing.title)
        self.q_res.put(flash_msg)
        session = self.storageDB.DBSession()
        new_listing = self.storageDB.Listings(
            id=listing.id,
            title=listing.title,
            category=listing.categories,
            description=listing.description,
            price=listing.unitprice,
            currency_code=listing.currency_code,
            qty_available=int(listing.quantity_available),
            order_max_qty=int(listing.order_max_qty),
            image_base64=listing.image_str,
            public=bool(listing.is_public),
            shipping_options=listing.shipping_options
            # TODO: Add other fields
        )
        session.add(new_listing)
        session.commit()
        time.sleep(0.1)

    def update_listing(self, listing):
        if listing.title != "":
            pass
        else:
            return False
        session = self.storageDB.DBSession()
        print "UPDATE LISTING DB " + listing.shipping_options
        db_listing = session.query(
            self.storageDB.Listings).filter_by(id=listing.id).first()
        db_listing.id = listing.id
        db_listing.title = listing.title
        db_listing.category = listing.categories
        db_listing.description = listing.description
        db_listing.price = listing.unitprice
        db_listing.currency_code = listing.currency_code
        db_listing.qty_available = int(listing.quantity_available)
        db_listing.order_max_qty = int(listing.order_max_qty)
        db_listing.image_base64 = listing.image_str
        db_listing.public = bool(listing.is_public)
        db_listing.shipping_options = listing.shipping_options
        session.commit()
        flash_msg = queue_task(
            0, 'flash_message', 'Updated listing ' + listing.title)
        self.q_res.put(flash_msg)
        time.sleep(0.1)

    def delete_listing(self, id):
        # Write the message to the database
        session = self.storageDB.DBSession()
        session.query(self.storageDB.Listings).filter_by(id=id).delete()
        session.commit()
        flash_msg = queue_task(0, 'flash_message', 'Deleted listing ')
        self.q_res.put(flash_msg)
        time.sleep(0.1)

    def read_configuration(self):
        # read configuration from database
        session = self.storageDB.DBSession()
        try:
            socks_proxy_enabled = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "socks_enabled").first()
            i2p_socks_proxy_enabled = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "i2p_socks_enabled").first()
            socks_proxy = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "proxy").first()
            socks_proxy_port = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "proxy_port").first()
            i2p_socks_proxy = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "i2p_proxy").first()
            i2p_socks_proxy_port = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "i2p_proxy_port").first()
            brokers = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "hubnodes").all()
            display_name = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "displayname").first()
            publish_identity = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "publish_identity").first()
            profile_text = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "profile").first()
            avatar_image = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "avatar_image").first()
            message_retention = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "message_retention").first()
            allow_unsigned = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "accept_unsigned").first()
            wallet_seed = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "wallet_seed").first()
            is_notary = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "is_notary").first()
            is_arbiter = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "is_arbiter").first()
            is_looking_glass = session.query(self.storageDB.Config.value).filter(
                self.storageDB.Config.name == "is_looking_glass").first()
        except:
            print "ERROR: Failed to read configuration from database"
            return False
        # Calculate btc master key from wallet seed
        if wallet_seed.value:
            self.btc_master_key = btc.bip32_master_key(wallet_seed.value)
        else:
            print "ERROR: Failed to generate Bitcoin master key, no seed found in database"
        # Tor SOCKS proxy
        self.proxy = socks_proxy.value
        self.proxy_port = socks_proxy_port.value
        if socks_proxy.value and socks_proxy_port.value and (socks_proxy_enabled.value == 'True'):
            self.proxy_enabled = bool(socks_proxy_enabled)
        else:
            self.proxy_enabled = False
        # i2p SOCKS proxy
        self.i2p_proxy = i2p_socks_proxy.value
        self.i2p_proxy_port = i2p_socks_proxy_port.value
        if i2p_socks_proxy.value and i2p_socks_proxy_port.value and (i2p_socks_proxy_enabled.value == 'True'):
            self.i2p_proxy_enabled = bool(i2p_socks_proxy_enabled)
        else:
            self.i2p_proxy_enabled = False
        self.brokers = brokers
        self.display_name = display_name.value
        if profile_text:
            self.profile_text = profile_text.value
        else:
            self.profile_text = ''
        if avatar_image:
            self.avatar_image = avatar_image.value
        else:
            self.avatar_image = ''
        self.retention_period = message_retention
        self.allow_unsigned = bool(allow_unsigned)
        if is_notary.value == 'True':
            self.is_notary = True
        if is_arbiter.value == 'True':
            self.is_arbiter = True
#        self.looking_glass = bool(is_looking_glass)    # TODO enable looking_glass mode based on database config
                                                        # TODO send a message to frontend to switch to looking glass mode
        if publish_identity.value:
            self.publish_identity = publish_identity
        # TODO: Read and assign all config options
        return True

    def get_items_from_listings(self, key_id, listings_dict):
        verified_listings = []
        # print "get_items from listing: " + listings_dict.__str__()
        print "get_items from listing from " + key_id
        for item in listings_dict:
            verify_item = self.gpg.verify(item)
            raw_item = item
            # remove the textwrapping we applied when encoding this submessage
#            item = item.replace('\n', '')
            if verify_item.key_id == key_id:  # TODO - this is a weak check - check the fingerprint is set
                try:
                    stripped_item = json_from_clearsigned(item)

                except:
                    stripped_item = ''
                    print "Error: item not extracted from signed sub-message"
                    continue
                try:
                    # TODO: Additional input validation required here
                    verified_item = json.loads(stripped_item)
                    print verified_item
                    # TODO: Additional input validation required here
                    item_shipping_options = json.loads(
                        verified_item['shipping_options'])
                    verified_item['shipping_options'] = item_shipping_options
                    verified_item['raw_contract'] = str(raw_item)
                    verified_listings.append(verified_item)
                except:
                    print "Error: item json not extracted from signed sub-message"
                    print item
                    continue
            else:
                print "Error: Item signture not verified for listing message held in cache db"
        return verified_listings

    def add_to_cart(self, item_id, key_id, sessionid):
        # TODO: At the moment this will only work if the listing has already
        # been retrieved (cached) - implement a fetch here if we need one
        session = self.storageDB.DBSession()
        item  = session.query(self.storageDB.cacheItems).filter(self.storageDB.cacheItems.key_id == key_id).filter(
            self.storageDB.cacheItems.id == item_id).first()
        if item:
            raw_msg = item.listings_block
            cart_res = item.__dict__
        else:
            print "ERROR: item could not be added to cart because it is not cached- try viewing the item first..."
            return False
#        cart_res = dict(msg)
        # TODO : error handling and input validation on this json
        item_from_msg = cart_res
        cart_db_res = session.query(self.storageDB.Cart).filter(self.storageDB.Cart.seller_key_id == key_id).filter(
            self.storageDB.Cart.item_id == cart_res['id']).count() > 0  # If it already exists then update
        if cart_db_res:
            print "Updating existing cart entry with another add"
            # TODO - since item is alrea in cart, we should add new quantity to
            # exsintng quantity  - for now we overwrite exisiting entry
            cart_entry = session.query(self.storageDB.Cart).filter(self.storageDB.Cart.seller_key_id == key_id).filter(
                self.storageDB.Cart.item_id == cart_res['id']).first()
#            cart_entry = session.query(self.storageDB.Cart).filter(self.storageDB.Cart.seller_key_id == key_id).filter(self.storageDB.Cart.item_id == cart_res['id']).update({
#                                                            self.storageDB.Cart.item_id:cart_res['id'],
#                                                            self.storageDB.Cart.raw_item:raw_msg,
#                                                            self.storageDB.Cart.item:item_from_msg,  # json.dumps(msg) # item:msg.__str__(),
#                                                            self.storageDB.Cart.quantity:(1 + cart_entry.quantity), # todo: add quantity to existing quantity
#                                                            self.storageDB.Cart.shipping:1
#                                                        })
            # cart_entry.iten_id = cart_res['id'] # this should not need to be
            # updated
            cart_entry.raw_item = raw_msg
            cart_entry.item = item_from_msg
            cart_entry.quantity = cart_entry.quantity + 1
            # calculate new line total price
            cart_entry.line_total_price = (int(cart_entry.quantity)  * float(cart_entry.price)) + float(json.loads(cart_entry.shipping_options)[cart_entry.shipping][1])

            # cart_entry.shipping = 1 # cart_res['shipping'] # leave shipping
            # as it is

        else:
            print "Adding new cart entry " + item.title
            cart_entry = self.storageDB.Cart(seller_key_id=key_id,
                                             item_id=cart_res['id'],
                                             raw_item=raw_msg,
                                             title=item.title,
                                             qty_available=item.qty_available,
                                             session_id=sessionid,
                                             order_max_qty = item.order_max_qty,
                                             price = item.price,
                                             currency_code = item.currency_code,
                                             # will hold json list of shipping options
                                             shipping_options = item.shipping_options,
                                             image_base64 = item.image_base64,
                                             publish_date = item.publish_date,
                                             seller_btc_stealth= item.seller_btc_stealth,

                                             quantity=1,  # ToDO read quantity from listing form if given
                                             shipping=1,

                                             # calculate new line total price
                                             line_total_price = (1  * float(item.price)) + float(json.loads(item.shipping_options)['1'][1])
            )  # ToDO read shipping choice from listing form if given                                                       )

            session.add(cart_entry)
        session.commit()

    def remove_from_cart(self, key_id):
        session = self.storageDB.DBSession()
        cart_entry = session.query(self.storageDB.Cart).filter(
            self.storageDB.Cart.seller_key_id == key_id).delete()
        session.commit()

    def update_cart(self, key_id, items, sessionid, transaction_type=None):
        # TODO error checking...
        session = self.storageDB.DBSession()
        for item in items:
            print "Update_cart is updating " + str(item) + " with " + str(items[item])
            cart_entry = session.query(self.storageDB.Cart).filter(
                self.storageDB.Cart.seller_key_id == key_id).filter(self.storageDB.Cart.item_id == item).first()
            (qty,shipping) = (items[item])
            cart_entry.quantity = qty
            cart_entry.shipping = shipping
            cart_entry.line_total_price = (int(cart_entry.quantity)  * float(cart_entry.price)) + float(json.loads(cart_entry.shipping_options)[cart_entry.shipping][1])
            cart_entry.session_id = sessionid
            if transaction_type:
                cart_entry.order_type = transaction_type
            session.commit()

    def create_order(self, key_id, buyer_address, buyer_note, sessionid):
        # TODO error checking...
        print "Creating order to seller " + key_id
        session = self.storageDB.DBSession()
        cart_entries = session.query(self.storageDB.Cart).filter(self.storageDB.Cart.seller_key_id == key_id).all()
        for entry in cart_entries:
            # generate an order for each line item
            orderid = ''.join(random.SystemRandom().choice(string.digits) for _ in range(9))
            if entry.order_type == 'direct':
                transient_seed = ''.join(random.SystemRandom().choice(string.ascii_letters + string.digits) for _ in range(64))
                transient_master = btc.bip32_master_key(transient_seed) # not needing to be strong
                transient_privkey = btc.bip32_extract_key(btc.bip32_ckd(transient_master, orderid))
                transient_pubkey = btc.privkey_to_pubkey(btc.bip32_extract_key(btc.bip32_ckd(transient_master, orderid)))
                seller_pubkey = btc.b58check_to_hex(entry.seller_btc_stealth)
                payment_address = sender_payee_address_from_stealth(transient_privkey,transient_pubkey)
                print payment_address
            elif entry.order_type == 'notarized':
                print "Warning - unsupported order type"
            elif entry.order_type == 'escrow':
                print "Warning - unsupported order type"
            new_order = self.storageDB.Orders ( orderid = orderid,
                                                session_id = sessionid,
                                                seller_key = key_id,
                                                buyer_key = self.mypgpkeyid,
                                                notary_key = None,
                                                buyer_ephemeral_btc_seed = transient_seed,
                                                buyer_btc_pub_key = transient_pubkey,
                                                payment_btc_address = payment_address,
                                                payment_status = 'unpaid', # unpaid/escrow/paid/refunded
                                                order_date = datetime.strptime(current_time(), "%Y-%m-%d %H:%M:%S"),
                                                order_status = 'created', # created/submitted/cancelled/rejected/processing/shipped/dispute/finalized
                                                is_synced = False,
                                                # is a sync time/date needed?or is a order history structure (json) needed to store event & date/time for all events
                                                delivery_address = buyer_address,
                                                order_note = buyer_note,
                                                order_type = entry.order_type,
                                                seller_btc_stealth = entry.seller_btc_stealth,
                                                item_id = entry.item_id,
                                                title = entry.title,
                                                price = entry.price,
                                                currency_code = entry.currency_code,
                                                shipping_options = entry.shipping_options,
                                                image_base64 = entry.image_base64,
                                                publish_date = entry.publish_date,
                                                raw_item = None,
                                                raw_seed = entry.raw_item,
                                                quantity = entry.quantity,
                                                shipping = entry.shipping,
                                                line_total_price = entry.line_total_price,
                                                line_total_btc_price = self.price_to_btc(entry.line_total_price,entry.currency_code) #
                                                )
            session.add(new_order)
            session.commit()
            # Call sync orders
            self.sync_orders()
            # Purge entry from shopping cart
            cart_entries = session.query(self.storageDB.Cart).filter(self.storageDB.Cart.seller_key_id == key_id).delete()
            session.commit()

        print "create_order finished..."

    def update_order(self,command,id,sessionid):
        # TODO : COnsider state machine once flow is fully defined
        if command == 'order_mark_paid':
            # this state is not transmitted to other parties and is used purely locally to help buyer
            session = self.storageDB.DBSession()
            order = session.query(self.storageDB.Orders).filter(self.storageDB.Orders.id == id).first()
            order.payment_status = 'confirming'
            session.commit()
            self.sync_orders()
        elif command == 'order_payment_received':
            # this state is not transmitted to other parties
            session = self.storageDB.DBSession()
            order = session.query(self.storageDB.Orders).filter(self.storageDB.Orders.id == id).first()
            order.payment_status = 'paid'
        elif command == 'order_shipped':
            # this state will be transmitted from seller back to buyer
            # TODO update order message and transmit to buyer
            session = self.storageDB.DBSession()
            order = session.query(self.storageDB.Orders).filter(self.storageDB.Orders.id == id).first()
            order.order_status = 'shipped'
            if self.mypgpkeyid == order.buyer_key:
                order.is_synced = True # we are the buyer, no need to send anything - the seller will see payment
            if self.mypgpkeyid == order.seller_key: # TODO should be self but this lets buyer and seller be the same- useful for testing for now
                order.is_synced = False # we are the seller, send shipped notice to buyer
            session.commit()
            self.sync_orders()
        elif command == 'order_finalize':
            print "Processing finalize state change"
            # this state will be transmitted from buyer back to seller
            # TODO update order message and transmit to buyer
            session = self.storageDB.DBSession()
            order = session.query(self.storageDB.Orders).filter(self.storageDB.Orders.id == id).first()
            order.order_status = 'finalized'
            if self.mypgpkeyid == order.seller_key: # TODO should be self but this lets buyer and seller be the same- useful for testing for now
                order.is_synced = True # we are the seller, do nothing for now
            if self.mypgpkeyid == order.buyer_key:
                order.is_synced = False # we are the buyer, send finalization notice - if notarized then prompt for order feedback
            session.commit()
            self.sync_orders()
        else:
            print "Error: Unknown order update type: " + command

    def sync_orders(self):
        # Send any order messages as necessary
        # iterate orderss table and send any unsent messages
        if not self.connected:
            return False
        session = self.storageDB.DBSession()
        orders = session.query(self.storageDB.Orders).filter(self.storageDB.Orders.is_synced == False).all()
        for order in orders:
            if order.order_status == 'created':
                ####### New order, needs to be sent #######
                order_msg = Message()
                order_msg.type = 'Order Message'
                order_msg.sender = self.mypgpkeyid
                order_msg.recipient = order.seller_key
                order_msg.subject = order.orderid # put the order id in the subject for easy processing of outbound messages
                order_dict = {}
                order_dict['id'] = order.orderid
                order_dict['quantity'] = order.quantity
                order_dict['shipping_option'] = order.shipping
                order_dict['delivery_address'] = order.delivery_address
                order_dict['buyer_notes'] = order.order_note
                order_dict['order_status'] = 'submitted'
                order_dict['order_type'] = order.order_type
                order_dict['buyer_btc_pub_key'] = order.buyer_btc_pub_key
                order_dict['payment_address'] = order.payment_btc_address # duplicate information but eases processing
                order_dict['currency_code'] = order.currency_code
                order_dict['total_price'] = order.line_total_price #
                order_dict['total_price_btc'] = order.line_total_btc_price
                order_dict['parent_contract_block'] = str(order.raw_seed)#.replace('\n', '\\n') # escape raw.seed becasue the pgp signature contains line breaks
                order_json = json.dumps(order_dict, sort_keys=True)
                order_json = textwrap.fill(
                    order_json, 80, drop_whitespace=False)
                signed_item = str(self.gpg.sign(
                    order_json, keyid=self.mypgpkeyid, passphrase=self.pgp_passphrase))
                order_msg.sub_messages.append(signed_item)
                order.raw_item = str(signed_item)#.replace('\n', '\\n')
#                order_out_message = Messaging.PrepareMessage(
#                    self.myMessaging, order_msg)
#                # send order message
#                if self.client.publish(self.pub_items, order_out_message, 1, True):
                order.order_status = 'submitted'
                session.commit()
                self.sendMessage(self.client,order_msg)
            elif order.order_status in ['rejected','processing','shipped','finalized','dispute','cancelled']:
                print "Order update message : " + order.order_status
                order_msg = Message()
                order_msg.type = 'Order Message'
                order_msg.sender = self.mypgpkeyid
                if order.order_status in ['rejected','processing','shipped','cancelled']:
                    # these state changes needs to be notified to the buyer
                    order_msg.recipient = order.buyer_key
                else:
                    # these state changes needs to be notified to the seller
                    order_msg.recipient = order.seller_key
                order_msg.subject = order.orderid # put the order id in the subject for easy processing of outbound messages
                order_dict = {}
                order_dict['parent_contract_block'] = str(order.raw_item)#.replace('\n', '\\n') # escape raw.seed becasue the pgp signature contains line breaks
                order_dict['order_status'] = order.order_status
                order_json = json.dumps(order_dict, sort_keys=True)
                order_json = textwrap.fill(
                    order_json, 80, drop_whitespace=False)
                signed_item = str(self.gpg.sign(
                    order_json, keyid=self.mypgpkeyid, passphrase=self.pgp_passphrase))
                order_msg.sub_messages.append(signed_item)
                session.commit()
                self.sendMessage(self.client,order_msg)

            else:
                print "Error: Unknown order state in order " + order.orderid + "=" + order.order_status

    def run(self):
        # TODO: Clean up this flow
        # make db connection
        print "NOTICE: Backend Thread Started"
        self.storageDB = Storage(self.dbsecretkey, "storage.db", self.appdir)
        if not self.storageDB.Start():
            print "Error: Unable to start storage database"
            # ' self.targetbroker)
            flash_msg = queue_task(
                0, 'flash_error', 'Unable to start storage database ' + 'storage.db')
            self.q_res.put(flash_msg)
            self.shutdown = True
        # read configuration from config table
        if not self.read_configuration():
            # ' self.targetbroker)
            flash_msg = queue_task(
                0, 'flash_error', 'Unable to read configuration from database ' + 'storage.db')
            self.q_res.put(flash_msg)
            self.shutdown = True
        # TODO: Execute database weeding functions here to include:
        # 1 - purge all PM's older than the configured retention period (unless message has been marked for retention)
        # 2 - purge addresses (buyer & seller side) for address information related to finalized transactions
        # -------------------------
        if self.is_notary:
            print "You are a notary"
        if self.is_arbiter:
            print "You are an arbiter"
        # are we selling anything?
        session = self.storageDB.DBSession()
        listings = session.query(self.storageDB.Listings).filter(self.storageDB.Listings.public==True).all()
        if listings:
            self.is_seller = True
            print "Public listings present"
        # COnfirm proxy settings
        # sort the broker list into Tor, i2p and clearnet
        for broker in self.brokers:
            broker = broker[0]
            if str(broker).endswith('.onion'):
                self.onion_brokers.append(broker)
            elif str(broker).endswith('.b32.i2p'):
                self.i2p_brokers.append(broker)
            else:   # There is a broker than appears to be neither Tor or i2p - we will not process these further for now unless test mode is enabled
                # TODO: check for test mode and permit clearnet RFC1918
                # addresses only if it is enabled - right now these will be
                # ignored
                self.clearnet_brokers.append(broker)
        if (not self.proxy_enabled) and (not self.i2p_proxy_enabled):
            flash_msg = queue_task(
                0, 'flash_error', 'WARNING: No Tor or i2p proxy specified. Setting off-line mode for your safety')
            self.q_res.put(flash_msg)
            self.workoffline = True
        # self.targetbroker = random.choice(self.brokers).value
        if not self.workoffline:
            # Select a random broker from our list of entry points and make
            # mqtt connection
            self.select_random_broker()
            if self.targetbroker.endswith('.onion'):
                self.client = mqtt(self.mypgpkeyid, False,
                              proxy=self.proxy, proxy_port=int(self.proxy_port))
                print self.proxy
                print self.proxy_port
                flash_msg = queue_task(
                    0, 'flash_message', 'Connecting to Tor hidden service ' + self.targetbroker)
                self.q_res.put(flash_msg)
            elif self.targetbroker.endswith('.b32.i2p'):
                self.client = mqtt(self.mypgpkeyid, False, proxy=self.i2p_proxy,
                              proxy_port=int(self.i2p_proxy_port))
                print self.i2p_proxy
                print self.i2p_proxy_port
                flash_msg = queue_task(
                    0, 'flash_message', 'Connecting to i2p hidden service ' + self.targetbroker)
                self.q_res.put(flash_msg)
            else:  # TODO: Only if in test mode
                if self.test_mode == True:
                    self.client = mqtt(self.mypgpkeyid, False,
                                  proxy=None, proxy_port=None)
                flash_msg = queue_task(
                    0, 'flash_error', 'WARNING: On-line mode enabled and target broker does not appear to be a Tor or i2p hidden service')
                self.q_res.put(flash_msg)
            # client = mqtt.Client(self.mypgpkeyid,False) # before custom mqtt
            # client # original paho-mqtt
            self.client.on_connect = self.on_connect
            self.client.on_message = self.on_message
            self.client.on_disconnect = self.on_disconnect
            self.client.on_publish = self.on_publish
            self.client.on_subscribe = self.on_subscribe
            # create broker authentication request
            password = self.make_pgp_auth()
            # print password
            self.client.username_pw_set(self.mypgpkeyid, password)
            flash_msg = queue_task(0, 'flash_status', 'Connecting...')
            self.q_res.put(flash_msg)
            try:
                self.connected = False
                # client.connect_async(self.targetbroker, 1883, 60)  # This is
                # now async
                self.client.connect(self.targetbroker, 1883, 30)  # This is now async
# time.sleep(0.5) # TODO: Find a better way to prevent the
# disconnect/reconnection loop following a connect

            except:  # TODO: Async connect now means this error code will need to go elsewhere
                self.on_disconnect(self, self.client, None) # todo: bit lazy buttakes care of a single reconnection attempt to another broker
                                                            # ideally we want to keep trying for x attempts - rewrite this all at some point
        while not self.shutdown:
            if not self.workoffline:
                self.client.loop(0.05)  # deal with mqtt events
            time.sleep(0.05)
#            if not self.connected and not self.workoffline and 1==0: # TODO - sort this!!
#                try:
#                    # create broker authentication request
#                    flash_msg = queue_task(0,'flash_status','Connecting...')
#                    self.q_res.put(flash_msg)
#                    password=self.make_pgp_auth()
#                    client.username_pw_set(self.mypgpkeyid,password)
#                    client.connect(self.targetbroker, 1883, 60) # todo: make this connect_async
#                    time.sleep(0.5) # TODO: Find a better way to prevent the disconnect/reconnection loop following a connect
#                    self.connected = True
#                except:
#                    # print "Could not connect to broker, will retry (main loop)"
#                    print "reconnect failed"
#                    pass

            for pending_message in self.task_state_messages.keys():   # check any messages queued for whatever reason
                if self.task_state_messages[pending_message]['message'].recipient == self.mypgpkeyid or self.task_state_messages[pending_message]['message'].recipient == "":
                    outbound = False
                    pending_key = self.task_state_messages[
                        pending_message]['message'].sender
                else:
                    outbound = True
                    pending_key = self.task_state_messages[
                        pending_message]['message'].recipient
                pending_message_state = self.task_state_messages[
                    pending_message]['state']
                # check pending message state
                if pending_message_state == MSG_STATE_NEEDS_KEY:
                    print "Need to request key " + pending_key
                    # create task request for a lookup
                    self.task_state_pgpkeys[pending_key][
                        'state'] = KEY_LOOKUP_STATE_INITIAL
                    self.task_state_messages[pending_message][
                        'state'] = MSG_STATE_KEY_REQUESTED
                elif pending_message_state == MSG_STATE_KEY_REQUESTED:
                    # print "Message is waiting on a key " + pending_key
                    if self.task_state_pgpkeys[pending_key]['state'] == KEY_LOOKUP_STATE_FOUND:
                        self.task_state_messages[pending_message][
                            'state'] = MSG_STATE_READY_TO_PROCESS
#                        del self.task_state_pgpkeys[pending_key]
                elif pending_message_state == MSG_STATE_QUEUED:
                    print "Can we send queued message for " + pending_key
                    if self.connected:
                        if outbound:  # this should always be true as incoming messages should never be set to MSG_STATE_QUEUED
                            self.sendMessage(self.client, self.task_state_messages[
                                             pending_message]['message'])
                            self.task_state_messages[pending_message][
                                'state'] = MSG_STATE_DONE
                elif pending_message_state == MSG_STATE_READY_TO_PROCESS:
                    print "Deferred message now ready"
                    if outbound:
                        print "Sending deferred message"
                        self.sendMessage(self.client, self.task_state_messages[
                                         pending_message]['message'])
                        self.task_state_messages[pending_message][
                            'state'] = MSG_STATE_DONE
                    else:
                        # This is an inbound message - throw it back at
                        # getmessage()
                        print "Re-processing received deferred message"
                        self.task_state_messages[pending_message][
                            'state'] = MSG_STATE_DONE
                        msg = MQTTMessage()
                        msg.payload = self.task_state_messages[
                            pending_message]['message'].raw_message
                        msg.topic = self.task_state_messages[
                            pending_message]['message'].topic
                        print msg.topic
                        self.on_message(self.client, None, msg)
#                        self.myMessaging.GetMessage(self.task_state_messages[pending_message]['message'].raw_message,self,allow_unsigned=self.allow_unsigned)

                elif pending_message_state == MSG_STATE_DONE:
                    del self.task_state_messages[pending_message]

            for pgp_key in self.task_state_pgpkeys.keys():   # initiate & monitor pgp key requests
                try:
                    state = self.task_state_pgpkeys[pgp_key]['state']
                except KeyError:
                    state = None
                if state == KEY_LOOKUP_STATE_INITIAL:
                    key_topic = 'user/' + pgp_key + '/key'
                    res = self.client.subscribe(str(key_topic), 1)
                    if res[0] == MQTT_ERR_SUCCESS:
                        self.task_state_pgpkeys[pgp_key][
                            'state'] = KEY_LOOKUP_STATE_REQUESTED
                        print "Subscribing to requested PGP key topic " + key_topic + " ...Subscribe Done"
                    else:
                        print "Subscribing to requested PGP key topic " + key_topic + " ...Subscribe Failed"
#                elif state == KEY_LOOKUP_STATE_REQUESTED:
#                    print "Waiting for key..."
#                elif state == KEY_LOOKUP_STATE_FOUND:
#                    print "Got key."
                elif state == KEY_LOOKUP_STATE_NOTFOUND:
                    print "Could not find a key OR unable to retrieve key"

            if not self.q.empty():
                task = self.q.get()
                if task.command == 'send_pm':
                    message = Message()
                    message.type = 'Private Message'
                    message.sender = self.mypgpkeyid
                    message.recipient = task.data['recipient']
                    message.subject = task.data['subject']
                    message.body = task.data['body']
                    message.sent = False
                    self.sendMessage(self.client, message)
                elif task.command == 'delete_pm':
                    message_to_del = task.data['id']
                    self.delete_pm(message_to_del)
                elif task.command == 'get_key':  # fetch a key from a user
                    print "Client Requesting key from backend"
                    self.task_state_pgpkeys[task.data['keyid']][
                        'state'] = KEY_LOOKUP_STATE_INITIAL  # create task request for a lookup
#                    key_topic = 'user/' + task.data['keyid'] + '/key'
# client.subscribe(str(key_topic),1) # disabked 24th July as duplicating
# code in the state_table
                elif task.command == 'get_profile':
                    key_topic = 'user/' + task.data['keyid'] + '/profile'
                    self.client.subscribe(str(key_topic), 1)
                    print "Requesting profile for " + task.data['keyid']
                elif task.command == 'get_listings':
                    item_id = task.data['id']
                    key_id = task.data['keyid']
                    key_topic = 'user/' + key_id + '/items'
                    self.client.subscribe(str(key_topic), 1)
                    print "Requesting listings for " + key_id

                elif task.command == 'add_to_cart':
                    item_id = task.data['item_id']
                    key_id = task.data['key_id']
                    sessionid = task.data['sessionid']
                    print "Backend received add to cart request for " + key_id + '/' + item_id + ' in lg session ' + sessionid
                    self.add_to_cart(item_id, key_id, sessionid)

                elif task.command == 'update_cart':
                    key_id = task.data['key_id']
                    items = task.data['items']
                    sessionid = task.data['sessionid']
                    self.update_cart(key_id, items,sessionid)

                elif task.command == 'checkout':
                    key_id = task.data['key_id']
                    items = task.data['items']
                    transaction_type = task.data['transaction_type']
                    sessionid = task.data['sessionid']
                    self.update_cart(key_id,items,sessionid,transaction_type=transaction_type) # first capture any updates made to the cart before processing checkout

                    print "Backend handled checkout message"
#                    self.checkout_cart(key_id)

                elif task.command == 'create_order':
                    key_id = task.data['key_id']
                    buyer_address = task.data['buyer_address']
                    buyer_note = task.data['buyer_note']
                    sessionid = task.data['sessionid']
                    self.create_order(key_id,buyer_address,buyer_note,sessionid)

                elif task.command == 'update_order':
                    id = task.data['id']
                    command = task.data['command']
                    sessionid = task.data['sessionid']
                    self.update_order(command,id,sessionid)

                elif task.command == 'remove_from_cart':
                    key_id = task.data['key_id']
                    print "Backend received delete from cart request for items from seller " + key_id
                    self.remove_from_cart(key_id)

                elif task.command == 'publish_listings':
                    listings_out_message = Messaging.PrepareMessage(
                        self.myMessaging, self.create_listings_msg())
                    # Our published items queue, qos=1, durable
                    self.client.publish(
                        self.pub_items, listings_out_message, 1, True)
                    flash_msg = queue_task(
                        0, 'flash_message', 'Re-publishing listings')
                    self.q_res.put(flash_msg)

                elif task.command == 'get_directory':
                        # Request list of all users
                    key_topic = 'user/+/directory'
                    self.client.subscribe(str(key_topic), 0)
                    print "Requesting directory of users"

                elif task.command == 'new_contact':
                    contact = Contact()
                    contact.displayname = task.data['displayname']
                    contact.pgpkey = task.data['pgpkey']
                    contact.pgpkeyid = task.data['pgpkeyid']
                    contact.flags = ''  # task.data['flags']
                    self.new_contact(contact)

                elif task.command == 'new_listing':
                    print "New listing: " + task.data['title']
                    listing = Listing()
                    listing.title = task.data['title']
                    listing.categories = task.data['category']
                    listing.description = task.data['description']
                    listing.unitprice = task.data['price']
                    listing.currency_code = task.data['currency']
                    listing.image_str = task.data['image']
                    listing.is_public = task.data['is_public']
                    listing.quantity_available = task.data['quantity']
                    listing.order_max_qty = task.data['max_order']
                    listing.shipping_options = task.data['shipping_options']
                    # TODO: add other listing fields
                    self.new_listing(listing)

                elif task.command == 'update_listing':
                    print "Update listing: " + task.data['title']
                    listing = Listing()
                    # Since we are updating the generate id needs to be
                    # overwritten
                    listing.id = task.data['id']
                    listing.title = task.data['title']
                    listing.categories = task.data['category']
                    listing.description = task.data['description']
                    listing.unitprice = task.data['price']
                    listing.currency_code = task.data['currency']
                    listing.image_str = task.data['image']
                    listing.is_public = task.data['is_public']
                    listing.quantity_available = task.data['quantity']
                    listing.order_max_qty = task.data['max_order']
                    listing.shipping_options = task.data['shipping_options']
                    # TODO: add other listing fields
                    self.update_listing(listing)

                elif task.command == 'delete_listing':
                    listing_to_del = task.data['id']
                    self.delete_listing(listing_to_del)

                elif task.command == 'shutdown':
                    self.shutdown = True
        try:
            self.client
        except NameError:
            pass
        else:
            if self.client._state == mqtt_cs_connected:
                self.client.disconnect()
        self.storageDB.DBSession.close_all()
        print "client-backend exits"
        # Terminated
