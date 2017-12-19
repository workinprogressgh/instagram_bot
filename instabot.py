from InstagramAPI.InstagramAPI import InstagramAPI
from persistqueue import SQLiteQueue

from sklearn.preprocessing import LabelEncoder,OneHotEncoder
from sklearn.exceptions import NotFittedError

from sklearn.linear_model import LogisticRegressionCV

from time import sleep
from copy import deepcopy
import threading
import random, requests, datetime, sys, pickle, os, yaml, json, logging
import numpy as np
import pandas as pd

LOGGER = logging.getLogger('instabot')

# modify API slightly so we can multithread properly
# breaks configureTimelineAlbum, direct_share, 
# getTotalFollowers, getTotalFollowings, getTotalUserFeed, getTotalLikedMedia
class ModifiedInstagramAPI(InstagramAPI):
	def SendRequest(self, endpoint, post = None, login = False):
		if (not self.isLoggedIn and not login):
			raise Exception("Not logged in!\n")
			return;
		self.s.headers.update ({'Connection' : 'close',
								'Accept' : '*/*',
								'Content-type' : 'application/x-www-form-urlencoded; charset=UTF-8',
								'Cookie2' : '$Version=1',
								'Accept-Language' : 'en-US',
								'User-Agent' : self.USER_AGENT})
		if (post != None): # POST
			response = self.s.post(self.API_URL + endpoint, data=post) # , verify=False
		else: # GET
			response = self.s.get(self.API_URL + endpoint) # , verify=False

		return response
	def login(self, force = False):
		if (not self.isLoggedIn or force):
			self.s = requests.Session()
			# if you need proxy make something like this:
			# self.s.proxies = {"https" : "http://proxyip:proxyport"}
			response = self.SendRequest('si/fetch_headers/?challenge_type=signup&guid=' + self.generateUUID(False), None, True)
			if response.status_code == 200:

				data = {'phone_id'   : self.generateUUID(True),
						'_csrftoken' : response.cookies['csrftoken'],
						'username'   : self.username,
						'guid'       : self.uuid,
						'device_id'  : self.device_id,
						'password'   : self.password,
						'login_attempt_count' : '0'}

				response = self.SendRequest('accounts/login/', self.generateSignature(json.dumps(data)), True)
				if response.status_code == 200:
					self.isLoggedIn = True
					self.username_id = json.loads(response.text)["logged_in_user"]["pk"]
					self.rank_token = "%s_%s" % (self.username_id, self.uuid)
					self.token = response.cookies["csrftoken"]

					self.syncFeatures()
					self.autoCompleteUserList()
					self.timelineFeed()
					self.getv2Inbox()
					self.getRecentActivity()
					print ("Login success!\n")
					return True;

class SlidingWindow:
	class Item:
		def __init__(self, value):
			self.value = value
			self.timestamp = datetime.datetime.now()
	def __init__(self, path, length = 3600, check_time = 120):
		self.length = datetime.timedelta(seconds=length)
		self.check_time = datetime.timedelta(seconds=check_time)
		self.last_check = datetime.datetime.now() - self.check_time * 2
		self.lock = threading.Lock()
		self.filepath = path+'/data.pkl'
		if not os.path.exists(path):
			os.makedirs(path)
		try:
			self._load()
		except:
			self.items = []
			self._save()
	def _save(self):
		with open(self.filepath, 'wb') as f:
			pickle.dump(self.items, f)
	def _load(self):
		with open(self.filepath, 'rb') as f:
			self.items = pickle.load(f)
	def _clean(self):
		# only clean every self.check_time
		if datetime.datetime.now() - self.last_check < self.check_time:
			return
		# remove old items
		self.lock.acquire()
		l = len(self.items)
		self.items = [i for i in self.items if (datetime.datetime.now()-i.timestamp) < self.length]
		if len(self.items) != l:
			self._save()
		self.lock.release()
	def __len__(self):
		self._clean()
		return len(self.items)
	def put(self, item):
		self._clean()
		self.lock.acquire()
		self.items += [self.Item(item)]
		self._save()
		self.lock.release()
	def get(self):
		self._clean()
		return [i.value for i in self.items]

class PriorityQueue:
	class Item:
		def __init__(self, value, priority):
			self.value = value
			self.priority = priority
	def __init__(self, size):
		self.items = []
		self.size = size
	def __len__(self):
		return len(self.items)
	def is_full(self):
		return len(self) >= self.size
	def put(self, item, priority):
		self.items += [self.Item(item, priority)]
		if len(self) > self.size:
			idx = np.argmin([i.priority for i in self.items])
			del self.items[idx]
	def get(self):
		if len(self) == 0: return None
		idx = np.argmax([i.priority for i in self.items])
		item = self.items[idx]
		del self.items[idx]
		return item.value

class InstaBot:
	def __init__(self, directory=''):

		self.directory = directory
		self.load_settings()

		self.targets_queue = PriorityQueue(self.max_hour_follows * 10)

		self.hour_likes = SlidingWindow(self.directory+'/hour_likes')
		self.hour_follows = SlidingWindow(self.directory+'/hour_follows')
		self.hour_unfollows = SlidingWindow(self.directory+'/hour_unfollows')

		self.target_data_path = self.directory+'/target_data/data.pkl'
		if not os.path.exists(self.directory+'/target_data'):
			os.makedirs(self.directory+'/target_data')
		try: 
			self.target_data = pickle.load(open(self.target_data_path,'rb'))
		except Exception as e:
			self.target_data = pd.DataFrame(
				columns=['user_id','timestamp','followers','followings','follow_back','tag','likes'])

		self.target_data_lock = threading.Lock()

		self.model = LogisticRegressionCV()

		self.api_lock = threading.Lock()

		self.api = ModifiedInstagramAPI(self.username, self.password)
		self.api.login()

	def load_settings(self):
		settings = yaml.load(open(self.directory+'/settings.yml','r'))
		for k,v in settings.items():
			setattr(self, k, v)

	def wait(self):
		t = np.random.exponential(self.mean_wait_time)
		sleep(t)

	def send_request(self, request, *args, **kwargs):
		self.api_lock.acquire()
		self.wait()
		ret = None
		LOGGER.debug("send_request; Reguest: "+request.__name__)
		LOGGER.debug("send_request; args: "+str(args))
		LOGGER.debug("send_request; kwargs: "+str(kwargs))
		try:
			response = request(*args, **kwargs)
			if response.status_code == 200:
				ret = json.loads(response.text)
			else:
				LOGGER.warning("send_request; Response:  "+str(response))
				if response.status_code != 404:
					LOGGER.warning("send_request; Response text:  "+str(response.text))
				if response.status_code in [400, 429]:
					try:
						if json.loads(response.text)['spam']:
							LOGGER.warning("send_request; Spam Detected")
							sleep(30*60)
					except:
						LOGGER.warning("send_request; Possible Rate Limiting Detected")
						sleep(5*60)

		except Exception as e:
			LOGGER.error("send_request; Exception: "+str(e))
		self.api_lock.release()
		return ret

	def get_tag_feed(self, tag):
		return self.send_request(self.api.tagFeed, tag)
	def get_user_feed(self, user_id):
		return self.send_request(self.api.getUserFeed, user_id)
	def like_media(self, media_id):
		return self.send_request(self.api.like, media_id)
	def follow_user(self, user_id):
		return self.send_request(self.api.follow, user_id)
	def unfollow_user(self, user_id):
		return self.send_request(self.api.unfollow, user_id)
	def get_user_info(self, user_id):
		ret = self.send_request(self.api.getUsernameInfo, user_id)
		if ret is None: return None
		return ret['user']
	def get_friendship_info(self, user_id):
		ret = self.send_request(self.api.userFriendship, user_id)
		if ret is None: return None
		return ret
	def followed_by(self, user_id):
		ret = self.get_friendship_info(user_id)
		try: 
			return ret['followed_by']
		except:
			return False

	def save_target_data(self):
		pickle.dump(self.target_data, open(self.target_data_path,'wb'), protocol=2)

	def update_target_data(self, row):
		if row[list(self.target_data.columns).index('user_id')] in self.target_data['user_id']:
			self.target_data.loc[self.target_data['user_id']==user_id,:] = row
		else:
			row = pd.Series(row, index=self.target_data.columns)
			self.target_data = self.target_data.append(row, ignore_index=True)

	def one_hot_encode(self, tags):
		tags = np.array(tags).reshape(-1)
		try:
			tags_users_list = self.tag_list + self.target_user_list
		except:
			tags_users_list = self.tag_list
		def tag_idx(tag):
			if tag in tags_users_list: return tags_users_list.index(tag)
			return len(tags_users_list)
		tags = [tag_idx(t) for t in tags]
		one_hot_tags = np.eye(len(tags_users_list)+1)[tags]
		return one_hot_tags

	def valid_target(self, user_id):
		friendship_info = self.get_friendship_info(user_id)
		try:
			return not(friendship_info['blocking'] or \
				friendship_info['followed_by'] or \
				friendship_info['following'] or \
				friendship_info['incoming_request'] or \
				friendship_info['outgoing_request'] or \
				friendship_info['is_private'])
		except:
			return False

	def get_following_follower_counts(self, user_id):
		user_info = self.get_user_info(user_id)
		if user_info is None: return (-1,-1)
		return (user_info['follower_count'], user_info['following_count'])

	# # # # # # # # # # 
	# Bot Workers
	# # # # # # # # # # 
	def model_fitter(self):

		def update_follow_backs():
			to_update = self.target_data.loc[
				(datetime.datetime.now()-self.target_data['timestamp'] > datetime.timedelta(days=1)) &
				pd.isnull(self.target_data['follow_back'])]

			for index, row in to_update.iterrows(): 
				user_id = row['user_id']
				follow_back = self.followed_by(user_id)
				self.target_data.loc[index, 'follow_back'] = follow_back

		def update_model():

			def get_model_data():
				useful_data = self.target_data.loc[
					~pd.isnull(self.target_data['follow_back'])]
				X = useful_data[['followers','followings']].as_matrix()
				tags = useful_data[['tag']].as_matrix()
				tags = self.one_hot_encode(tags)
				X = np.append(np.log1p(X.astype(float)), tags, axis=1)
				y = useful_data['follow_back'].as_matrix().astype(int)
				return X,y

			X,y = get_model_data()
			if len(np.unique(y)) > 1:
				m = deepcopy(self.model)
				m.fit(X,y)
				self.model = m
				#return cross_val_score(m, X, y, n_jobs=1).mean()
			return 0

		while True:
			LOGGER.info("fit_model: update_model")
			score = update_model()
			#LOGGER.info("fit_model: cross_val_score "+'{0:.3f}'.format(score))
			LOGGER.info("fit_model: update_follow_backs")
			update_follow_backs()
			LOGGER.info("fit_model: save_target_data")
			self.save_target_data()
			LOGGER.info("fit_model: sleep")
			sleep(60*60)

	def info_printer(self):
		followed_queue = SQLiteQueue(self.directory+'/followed_users')
		while True:
			print(datetime.datetime.now().strftime('%x %X'))
			print("  Followed Users:", len(followed_queue))
			print("  Hour Likes    :", len(self.hour_likes))
			print("  Hour Follows  :", len(self.hour_follows))
			print("  Hour Unfollows:", len(self.hour_unfollows))
			print("  Targets Queue:")
			print("    Total Len:", len(self.targets_queue))
			try:
				priorities = sorted([i.priority for i in self.targets_queue.items], reverse=True)
				qs = np.percentile(priorities[:int(self.max_hour_follows)], [100,50,0])
				print("    Top-"+str(self.max_hour_follows)+" Max-Med-Min:", 
					['{0:.2f}'.format(x) for x in qs])
			except:
				pass
			try:
				if False:
					print("  Model Coefficients:")
					print("    Intercept Odds Ratio:", 
						'{0:.2e}'.format(np.exp(self.model.intercept_[0])))
					print("    Coefficient Odds Ratios:")
					l = ['followers','followings'] + self.tag_list
					for tag,coef in zip(l, self.model.coef_[0][2:]):
						print("      "+tag.rjust(len(max(l, key=len)))+": 1"+\
							'{0:+.1e}'.format(np.exp(coef)-1))
			except: 
				pass
			print("  Thread Alive:")
			print("    fit_model           :", self.fit_model_thread.is_alive())
			print("    find_targets        :", self.find_targets_thread.is_alive())
			print("    like_follow_unfollow:", self.like_follow_unfollow_thread.is_alive())
			print("  Refresh settings")
			self.load_settings()
			sleep(15*60)

	def target_finder(self):

		def select_tag():
			return random.choice(self.tag_list)
		def select_target_username():
			return random.choice(target_user_iterators)

		def get_followback_confidence(user_info):
			x = [user_info['followers'], user_info['followings']]
			x = np.reshape(x,(1,-1))
			tag = self.one_hot_encode([user_info['tag']])
			x = np.append(np.log1p(x.astype(float)), tag, axis=1)
			try:
				followback_confidence = \
					self.model.predict_proba(x)[0,list(self.model.classes_).index(1)]
			except (NotFittedError, xgb.core.XGBoostError):
				LOGGER.warning("get_followback_confidence, model not fitted")
				followback_confidence = 1
			return followback_confidence

		# create iterators for each target user
		target_user_iterators = []
		try:
			for username in self.target_user_list:
				LOGGER.info("find_targets: create_user_iterator")
				try:
					user_info = self.send_request(self.api.searchUsername, username)
					user_id = user_info['user']['pk']
					follower_list = self.send_request(self.api.getUserFollowers, user_id)
					user_ids = [user['pk'] for user in follower_list['users']]
					iterator = iter(user_ids)
					target_user_iterators += [(iterator, username)]
				except Exception as e:
					LOGGER.error("find_targets, create_user_iterator; Exception: "+str(e))
		except Exception as e:
			LOGGER.error("find_targets, create_user_iterators; Exception: "+str(e))

		# build up self.targets_queue
		while True:
			for _ in range(self.max_hour_follows):
				try:
					# select target_user_list or tag_list
					if len(target_user_iterators) == 0 or np.random.rand() < 0.5:
						# get user_ids from tag feed
						LOGGER.info("find_targets: select_tag")
						tag = select_tag()
						items = self.get_tag_feed(tag)
						user_ids = [item['user']['pk'] for item in items['items']]
					else:
						# get user_ids from target user followers
						LOGGER.info("find_targets: select_target_username")
						idx = np.random.randint(0,len(target_user_iterators))
						user_ids, tag = target_user_iterators[idx]
						if not(any(tag)):
							del target_user_iterators[idx]
							raise Exception("find_targets: user_ids iterator empty")

					# iterate over user_ids
					for i,user_id in enumerate(user_ids):
						if i >= 5: break
						LOGGER.info("find_targets: valid_target")
						if self.valid_target(user_id):
							LOGGER.info("find_targets: get_following_follower_counts")
							user_followers, user_followings = \
								self.get_following_follower_counts(user_id)

							user_info = {
								'user_id':user_id,
								'followers':user_followers,
								'followings':user_followings,
								'likes':0,
								'tag':tag,
								'discovery_time':datetime.datetime.now()}

							LOGGER.info("find_targets: get_followback_confidence")
							followback_confidence = get_followback_confidence(user_info)

							# pseudo epsilon greedy strategy
							# aim for every 1/10 targets being random
							# explore ~20 targets for every 1 real target
							epsilon = 0.1/20
							if np.random.rand() < epsilon:
								LOGGER.info("find_targets: mark target for exploration")
								followback_confidence = 1

							self.targets_queue.put(user_info, followback_confidence)
				except Exception as e:
					LOGGER.error("find_targets; Exception: "+str(e))
			LOGGER.info("find_targets: sleep")
			sleep(15*60)

	def like_follow_unfollow(self):

		def target_users():

			def target_user(user_id):
				items = self.get_user_feed(user_id)
				if items is None: 
					LOGGER.warning("like_follow_unfollow: target_users: target_user: user feed 404")
					return

				# like
				if self.likes_per_user > 0:
					for i,item in enumerate(items['items']):
						if len(self.hour_likes) >= self.likes_per_user * (len(self.hour_follows)+1):
							break

						media_id = item['pk']

						if not(item['has_liked']):
							LOGGER.info("like_follow_unfollow: target_users: target_user: like")
							self.like_media(media_id)
							self.hour_likes.put(media_id)

				# follow
				if self.max_hour_follows > 0 and self.max_followed > 0:
					LOGGER.info("like_follow_unfollow: target_users: target_user: follow")
					self.follow_user(user_id)
					self.hour_follows.put(user_id)
					followed_queue.put(user_id)

			while len(self.hour_follows) < self.max_hour_follows:
				user_info = self.targets_queue.get()
				if user_info is not None:
					user_id = user_info['user_id']
					if self.valid_target(user_id):

						LOGGER.info("like_follow_unfollow: target_users: target_user")
						target_user(user_info['user_id'])

						if user_info['followers'] != -1 and \
							user_info['followings'] != -1:
							row = (user_info['user_id'], datetime.datetime.now(), 
									user_info['followers'], user_info['followings'], np.nan,
									user_info['tag'], user_info['likes'])
							self.update_target_data(row)

		def unfollow_users():
			while (len(followed_queue) > self.max_followed) and \
				(len(self.hour_unfollows) < self.max_hour_follows*1.1):
				user_id = followed_queue.get()
				LOGGER.info("like_follow_unfollow: unfollow_users: unfollow")
				ret = self.unfollow_user(user_id)
				if ret is None: 
					LOGGER.info("like_follow_unfollow: unfollow_users: unfollow fail")
					followed_queue.put(user_id)
				self.hour_unfollows.put(user_id)

		followed_queue = SQLiteQueue(self.directory+'/followed_users')
		while True:
			LOGGER.info("like_follow_unfollow: target_users")
			target_users()
			LOGGER.info("like_follow_unfollow: unfollow_users")
			unfollow_users()
			LOGGER.info("like_follow_unfollow: sleep")
			sleep(5*60)

	def run(self):

		# model data gathering and fitting
		self.fit_model_thread = threading.Thread(
			target=self.model_fitter)
		self.fit_model_thread.start()

		# locate potential targets
		self.find_targets_thread = threading.Thread(
			target = self.target_finder)
		self.find_targets_thread.start()

		# likes, follows and unfollows
		self.like_follow_unfollow_thread = threading.Thread(
			target=self.like_follow_unfollow)
		self.like_follow_unfollow_thread.start()

		# print information
		self.print_info_thread = threading.Thread(
			target=self.info_printer)
		self.print_info_thread.start()


# usage: python3 instabot.py <USERNAME>
if __name__ == '__main__':

	username = sys.argv[1]

	formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
	handler = logging.FileHandler(username+'/debug.log', mode='w')
	handler.setFormatter(formatter)

	LOGGER.addHandler(handler)
	logging.getLogger('requests').addHandler(handler)
	LOGGER.setLevel(logging.DEBUG)
	logging.getLogger('requests').setLevel(logging.WARNING)

	bot = InstaBot(username)
	bot.run()
