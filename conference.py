#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'

from datetime import datetime
import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.ext import ndb
from google.appengine.api import memcache
from google.appengine.api import taskqueue

from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import TeeShirtSize
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForms
from models import BooleanMessage
from models import ConflictException
from models import StringMessage
from models import Session
from models import SessionForm
from models import SessionForms

from utils import getUserId

from settings import WEB_CLIENT_ID

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": ["Default", "Topic"],
}

SESS_DEFAULTS = {
    "typeOfSession": "Generic",
    "highlights": "Unknown",
    "duration": "30",
}

OPERATORS = {
    'EQ': '=',
    'GT': '>',
    'GTEQ': '>=',
    'LT': '<',
    'LTEQ': '<=',
    'NE': '!='
}

FIELDS = {
    'CITY': 'city',
    'TOPIC': 'topics',
    'MONTH': 'month',
    'MAX_ATTENDEES': 'maxAttendees',
}

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

SESS_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
    typeOfSession=messages.StringField(2, required=True),
)

SESS_POST_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeConferenceKey=messages.StringField(1, required=True),
)

SPKR_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    speaker=messages.StringField(1, required=True),
)

SESS_BEFORE_EXLUDING_POST_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    lastHour=messages.StringField(1, required=True),
    sessType=messages.StringField(2, required=True),
)

WISH_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeSessionKey=messages.StringField(1, required=True),
)

MEMCACHE_ANNOUNCEMENTS_KEY = 'Recent Announcements'
MEMCACHE_SPEAKER_KEY = "FEATURED_SPEAKER"
# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

@endpoints.api(name='conference',
               version='v1',
               allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID],
               scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

    # - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf

    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)

        profile = p_key.get()
        if not profile:
            profile = Profile(
                # userId = None,
                key=p_key,
                displayName=user.nickname(),
                mainEmail=user.email(),
                teeShirtSize=str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()

        return profile  # return Profile

    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
            prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)

    @endpoints.method(message_types.VoidMessage, ProfileForm,
                      path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()

    @endpoints.method(ProfileMiniForm, ProfileForm,
                      path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)

    # - - - Conference objects - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf

    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        # both for data model & outbound Message
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
            setattr(request, "seatsAvailable", data["maxAttendees"])

        # make Profile Key from user ID
        p_key = ndb.Key(Profile, user_id)
        # allocate new Conference ID with Profile key as parent
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        # make Conference key from ID
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference & return (modified) ConferenceForm
        Conference(**data).put()

        return request

    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
                      http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)

    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q

    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)

    @endpoints.method(ConferenceQueryForms, ConferenceForms,
                      path='queryConferences',
                      http_method='POST',
                      name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

        # return individual ConferenceForm object per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") \
                   for conf in conferences]
        )

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
                      path='getConferencesCreated',
                      http_method='POST', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # make profile key
        p_key = ndb.Key(Profile, getUserId(user))
        # create ancestor query for this user
        conferences = Conference.query(ancestor=p_key)
        # get the user profile and display name
        prof = p_key.get()
        displayName = getattr(prof, 'displayName')
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, displayName) for conf in conferences]
        )

    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
                      path='conference/{websafeConferenceKey}',
                      http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        prof = conf.key.parent().get()

        if not prof:
            raise endpoints.NotFoundException('Not able to get prof')

        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
                      path='filterPlayground',
                      http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        q = Conference.query()
        # simple filter usage:
        # q = q.filter(Conference.city == "Paris")

        # advanced filter building and usage
        field = "city"
        operator = "="
        value = "London"
        f = ndb.query.FilterNode(field, operator, value)
        # q = q.filter(f)
        # add 2 filters:
        # 1: city equals to London
        # 2: topic equals "Medical Innovations"
        field2 = "topics"
        operator2 = "in"
        value2 = "Medical Innovations"
        #  f2 = ndb.query.FilterNode(field2, operator2, value2)
        q = q.filter(f).filter(Conference.topics.IN([value2]))
        q = q.order(Conference.name)
        q = q.filter(Conference.maxAttendees > 50)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )

    # - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser()  # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)

    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
                      path='conference/{websafeConferenceKey}',
                      http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
                      path='conferences/attending',
                      http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""

        prof = self._getProfileFromUser()
        conf_keys = [ndb.Key(urlsafe=wsck) for wsck in prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf, "") \
                                      for conf in conferences]
                               )

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = '%s %s' % (
                'Last chance to attend! The following conferences '
                'are nearly sold out:',
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement

    @endpoints.method(message_types.VoidMessage, StringMessage,
                      path='conference/announcement/get',
                      http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        # return an existing announcement from Memcache or an empty string.
        announcement = memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY)
        return StringMessage(data=announcement)

    # - - - Session Addition  - - - - - - - - - - - - - - - - - - - -

    def _copySessionToForm(self, sess):
        """Copy relevant fields from Session to SessionForm."""
        sf = SessionForm()
        for field in sf.all_fields():
            if hasattr(sess, field.name):
                # convert Date/Time to date/time string; just copy others
                if field.name.endswith(('date', 'startTime')):
                    setattr(sf, field.name, str(getattr(sess, field.name)))
                else:
                    setattr(sf, field.name, getattr(sess, field.name))
            elif field.name == "websafeKey":
                setattr(sf, field.name, sess.key.urlsafe())
        sf.check_initialized()
        return sf

    def _createSessionObject(self, request):
        """Create or update Session object, returning SessionForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # get the existing conference from the key submitted
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

            # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the conference owner can add new sessions.')

        # copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}

        # make a new session key based on the conf we grabbed earlier as the parent
        conf_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        # make a session id and key using the conference key submitted as the parent
        sess_id = Session.allocate_ids(size=1, parent=conf_key)[0]
        sess_key = ndb.Key(Session, sess_id, parent=conf_key)
        # store the new session key in the data
        data['key'] = sess_key
        # remove the our submitted keys from the data as they are not needed now
        del data['websafeKey']
        del data['websafeConferenceKey']

        # add default values for those missing (both data model & outbound Message)
        for df in SESS_DEFAULTS:
            if data[df] in (None, []):
                data[df] = SESS_DEFAULTS[df]
                setattr(request, df, SESS_DEFAULTS[df])

        # convert dates from string to Date object
        if data['date']:
            data['date'] = datetime.strptime(data['date'][:10], "%Y-%m-%d").date()

        # convert startTime from string to Time object
        if data['startTime']:
            data['startTime'] = datetime.strptime(data['startTime'][:5], "%H:%M").time()

        # generate Profile Key based on user ID and Session
        # ID based on Profile key get Session key from ID
        p_key = ndb.Key(Profile, user_id)
        c_id = Session.allocate_ids(size=1, parent=p_key)[0]
        c_key = ndb.Key(Session, c_id, parent=p_key)
        data['key'] = sess_key

        # creation of Session & return (modified) SessionForm
        Session(**data).put()

        taskqueue.add(params={'email': user.email(),
                              'conferenceInfo': repr(request)},
                      url='/tasks/send_confirmation_email'
                      )

        taskqueue.add(params={'speaker': data['speaker']},
                      url='/tasks/check_and_add_featured_speaker'
                      )
        return request

    @endpoints.method(SessionForm, SessionForm, path='session',
                      http_method='POST', name='createSession')
    def createSession(self, request):
        """Create new session.  open to the organizer of the conference"""
        return self._createSessionObject(request)

    @endpoints.method(CONF_GET_REQUEST, SessionForms,
                      path='conference/{websafeConferenceKey}/getSessions',
                      http_method='GET', name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Given a conference, return all sessions"""
        # get Conference object from request; bail if not found
        conf_key = ndb.Key(urlsafe=request.websafeConferenceKey)

        if not conf_key:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # ancestor query for this conference key
        sessions = Session.query(ancestor=conf_key)

        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessions]
        )

    @endpoints.method(SESS_GET_REQUEST, SessionForms,
                      path='conference/{websafeConferenceKey}/getSessionsByType/{typeOfSession}',
                      http_method='GET', name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Given a conference, return all sessions of a specified type"""
        # get Conference object from request; bail if not found
        conf_key = ndb.Key(urlsafe=request.websafeConferenceKey)

        if not conf_key:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # ancestor query for this conference key
        sessions = Session.query(ancestor=conf_key).filter(Session.typeOfSession == request.typeOfSession)

        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessions]
        )

    @staticmethod
    def _cacheFeaturedSpeaker(speaker):
        """Check if the specified speaker has more than 1 session
        and if so, cache them in Memcache as the featured speaker"""
        # get all sessions with this speaker listed and parse it
        speakerSessions = Session.query(Session.speaker == speaker)
        speakerSessionNames = [sess.name for sess in speakerSessions]
        # if there is more than one session for this speaker, join them all
        # back together with the speaker name and store it in memcache
        if len(speakerSessionNames) > 1:
            cache_string = speaker + ': ' + ', '.join(speakerSessionNames)
            memcache.set(MEMCACHE_SPEAKER_KEY, cache_string)

    @endpoints.method(SPKR_GET_REQUEST, SessionForms,
                      path='sessions/{speaker}',
                      http_method='GET', name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Given a speaker, return all sessions given by this particular
         speaker, across all conferences"""
        sessions = Session.query(Session.speaker == request.speaker)

        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessions]
        )

    @endpoints.method(message_types.VoidMessage, StringMessage,
                      path='getFeaturedSpeaker',
                      http_method='GET', name='getFeaturedSpeaker')
    def getFeaturedSpeaker(self, request):
        """Feetches featured speaker with sessions from memcache."""
        return StringMessage(data=memcache.get(MEMCACHE_SPEAKER_KEY) or "")

    @endpoints.method(SESS_BEFORE_EXLUDING_POST_REQUEST, SessionForms,
                      path='getSessionsBeforeHourExcludingType/{lastHour}/{sessType}',
                      http_method='POST',
                      name='getSessionsBeforeHourExcludingType')
    def getSessionsBeforeHourExcludingType(self, request):
        """Get list of sessions before a certain hour, excluding a single type"""

        # get all sessions that begin before the specified time (including ones with no time)
        earlySessions = Session.query(ndb.OR(
            Session.startTime == None,
            Session.startTime <= datetime.strptime(request.lastHour[:2], "%H").time()
        ))


        # filter out the sessions of the specified type
        result_sessions = []
        for sess in earlySessions:
            if request.sessType.lower() not in sess.typeOfSession.lower():
                result_sessions.append(sess)

        # return all of the sessions not removed by our filter
        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in result_sessions]
        )

    @endpoints.method(message_types.VoidMessage, SessionForms,
                      http_method='GET', name='getGenericSessions')
    def getGenericSessions(self, request):
        """Returns sessions still using the default Generic type"""

        sessions = Session.query(Session.typeOfSession == 'Generic')

        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessions]
        )

    @endpoints.method(message_types.VoidMessage, SessionForms,
                      http_method='GET', name='getUnknownHighlightsSessions')
    def getUnknownHighlightsSessions(self, request):
        """Returns sessions still using the default Unknown highlights"""

        sessions = Session.query(Session.highlights == 'Unknown')

        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessions]
        )

    @ndb.transactional(xg=True)
    @endpoints.method(WISH_REQUEST, SessionForm,
                      http_method='POST', name='addSessionToWishlist')
    def addSessionToWishlist(self, request):
        """Saves a session to a user's wishlist"""
        # ensure only logged in users are able to request this
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('You must be logged in to do this')

        # get session from the key and ensure it exists
        sess = ndb.Key(urlsafe=request.websafeSessionKey).get()

        # check that session exists
        if not sess:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % request.websafeSessionKey)

        # get user profile
        prof = self._getProfileFromUser()

        # If this is a duplicate, throw an exception
        if request.websafeSessionKey in prof.sessionsToAttend:
            raise endpoints.BadRequestException(
                'Session already in the wishlist')

        # append to user profile's wishlist
        prof.sessionsToAttend.append(request.websafeSessionKey)
        prof.put()

        return self._copySessionToForm(sess)

    @endpoints.method(message_types.VoidMessage, SessionForms,
                      http_method='POST', name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Returns sessions in the current user's wishlist"""
        # ensure we have a user already
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # get the wishlist from the user's profile
        prof = self._getProfileFromUser()
        sess_keys = [ndb.Key(urlsafe=sess_key) for sess_key in prof.sessionsToAttend]
        # sessions = ndb.get_multi(sess_keys)
        sessions = ndb.get_multi(sess_keys)

        # return set
        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessions]
        )

    @ndb.transactional(xg=True)
    @endpoints.method(WISH_REQUEST, BooleanMessage,
                      path='wishlist/{websafeSessionKey}',
                      http_method='DELETE', name='deleteSessionInWishlist')
    def removeSessionFromWishlist(self, request):
        """Remove the specified session from wishlist."""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('You must be logged in to do this')

        # get session from the key and ensure it exists
        sess = ndb.Key(urlsafe=request.websafeSessionKey).get()

        # check that session exists
        if not sess:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % request.websafeSessionKey)

        # get user profile
        prof = self._getProfileFromUser()

        retVal = False
        if request.websafeSessionKey in prof.sessionsToAttend:
            prof.sessionsToAttend.remove(request.websafeSessionKey)
            retVal = True
            prof.put()
            sess.put()
        return BooleanMessage(data=retVal)

# registers API
api = endpoints.api_server([ConferenceApi])
