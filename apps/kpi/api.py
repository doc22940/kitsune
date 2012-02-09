from operator import itemgetter
from datetime import date, timedelta

from django.db import connections, router
from django.db.models import Count, F

from tastypie import fields
from tastypie.authentication import BasicAuthentication
from tastypie.authorization import Authorization
from tastypie.cache import SimpleCache
from tastypie.resources import Resource

from kpi.models import Metric, MetricKind
from questions.models import Question, Answer, AnswerVote
from wiki.models import HelpfulVote, Revision


class CachedResource(Resource):
    def obj_get_list(self, request=None, **kwargs):
        """Override ``obj_get_list`` to use the cache."""
        cache_key = self.generate_cache_key('list', **kwargs)
        obj_list = self._meta.cache.get(cache_key)

        if obj_list is None:
            obj_list = self.get_object_list(request)
            self._meta.cache.set(cache_key, obj_list, timeout=60 * 60 * 3)

        return obj_list


class WriteOnlyBasicAuthentication(BasicAuthentication):
    """Authenticator that prompts for credentials only for write requests."""
    def is_authenticated(self, request, **kwargs):
        if request.method == 'GET':
            return True
        return super(WriteOnlyBasicAuthentication, self).is_authenticated(
                request, **kwargs)


class PermissionAuthorization(Authorization):
    """Authorization which allows all users to make read-only requests and
    users with a certain permission to write."""

    def __init__(self, write=None):
        self.write_perm = write

    def is_authorized(self, request, object=None):
        # Supports just GET and POST so far
        if request.method == 'GET':
            return True
        elif request.method == 'POST':
            return request.user.has_perm(self.write_perm)
            # TODO: What about basic auth?
        return False


class Struct(object):
    """Convert a dict to an object"""
    def __init__(self, **entries):
        self.__dict__.update(entries)

    def __unicode__(self):
        return unicode(self.__dict__)


class SearchClickthroughMeta(object):
    """Abstract Meta inner class for search clickthrough resources"""
    # The Resource metaclass isn't smart enough to merge in attributes from
    # Meta classes defined in subclasses of an abstract resource, like Django's
    # ORM metaclass is. There's might be a supported way, but I'm really sick
    # of reading tastypie docs (and source).
    object_class = Struct
    authentication = WriteOnlyBasicAuthentication()
    authorization = PermissionAuthorization(write='users.change_metric')


class SearchClickthroughResource(CachedResource):
    """Clickthrough ratio for Sphinx or Elastic searches for one period

    Represents a ratio of {clicks of results}/{total searches} for one engine.

    """
    #: Date of period start. Assumes 1-week periods.
    start = fields.DateField('start')
    #: How many searches had (at least?) 1 result clicked
    clicks = fields.IntegerField('clicks', default=0)
    #: How many searches were performed with this engine
    searches = fields.IntegerField('searches', default=0)

    @property
    def searches_kind(self):
        return 'search clickthroughs:%s:searches' % self.engine

    @property
    def clicks_kind(self):
        return 'search clickthroughs:%s:clicks' % self.engine

    def obj_get(self, request=None, **kwargs):
        """Fetch a particular ratio by start date."""
        raise NotImplementedError

    def get_object_list(self, request):
        """Return the authZ-limited set of ratios"""
        return self.obj_get_list(request)

    def obj_get_list(self, request=None, **kwargs):
        """Return all the ratios.

        Or, if a ``min_start`` query param is present, return the (potentially
        limited) ratios later than or equal to that. ``min_start`` should be
        something like ``2001-07-30``.

        If, somehow, half a ratio is missing, that ratio is not returned.

        """
        # Make min_start either a date or None:
        min_start = request.GET.get('min_start')
        if min_start:
            try:
                min_start = _parse_date()
            except (ValueError, TypeError):
                pass

        # I'm not sure you can join a table to itself with the ORM.
        cursor = _cursor()
        cursor.execute(  # n for numerator, d for denominator
            'SELECT n.start, n.value, d.value '
            'FROM kpi_metric n '
            'INNER JOIN kpi_metric d ON n.start=d.start '
            'WHERE n.kind_id=(SELECT id FROM kpi_metrickind WHERE code=%s) '
            'AND d.kind_id=(SELECT id FROM kpi_metrickind WHERE code=%s) ' +
            ('AND n.start>=%s' if min_start else ''),
            (self.clicks_kind, self.searches_kind) +
                ((min_start,) if min_start else ()))
        return [Struct(start=s, clicks=n, searches=d) for
                s, n, d in cursor.fetchall()]

    def obj_create(self, bundle, request=None, **kwargs):
        def create_metric(kind, value_field, data):
            """Given POSTed data, create a Metric.

            Assume week-long buckets for the moment.

            """
            start = date(*_parse_date(data['start']))
            Metric.objects.create(kind=MetricKind.objects.get(code=kind),
                                  start=start,
                                  end=start + timedelta(days=7),
                                  value=data[value_field])

        create_metric(self.searches_kind, 'searches', bundle.data)
        create_metric(self.clicks_kind, 'clicks', bundle.data)

    def get_resource_uri(self, bundle_or_obj):
        """Return a fake answer; we don't care, for now."""
        return ''


class SphinxClickthroughResource(SearchClickthroughResource):
    engine = 'sphinx'

    class Meta(SearchClickthroughMeta):
        resource_name = 'sphinx-clickthrough-rate'


class ElasticClickthroughResource(SearchClickthroughResource):
    engine = 'elastic'

    class Meta(object):
        resource_name = 'elastic-clickthrough-rate'


class SolutionResource(CachedResource):
    """
    Returns the number of questions with
    and without an answer maked as the solution.
    """
    date = fields.DateField('date')
    solved = fields.IntegerField('solved', default=0)
    questions = fields.IntegerField('questions', default=0)

    def get_object_list(self, request):
        # Set up the query for the data we need
        qs = _qs_for(Question)

        # Filter on solution
        qs_with_solutions = qs.filter(solution__isnull=False)

        return merge_results(solved=qs_with_solutions, questions=qs)

    class Meta(object):
        cache = SimpleCache()
        resource_name = 'kpi_solution'
        allowed_methods = ['get']


class VoteResource(CachedResource):
    """
    Returns the number of total and helpful votes for Articles and Answers.
    """
    date = fields.DateField('date')
    kb_helpful = fields.IntegerField('kb_helpful', default=0)
    kb_votes = fields.IntegerField('kb_votes', default=0)
    ans_helpful = fields.IntegerField('ans_helpful', default=0)
    ans_votes = fields.IntegerField('ans_votes', default=0)

    def get_object_list(self, request):
        # Set up the queries for the data we need
        qs_kb_votes = _qs_for(HelpfulVote)
        qs_ans_votes = _qs_for(AnswerVote)

        # Filter on helpful
        qs_kb_helpful_votes = qs_kb_votes.filter(helpful=True)
        qs_ans_helpful_votes = qs_ans_votes.filter(helpful=True)

        return merge_results(
                    kb_votes=qs_kb_votes,
                    kb_helpful=qs_kb_helpful_votes,
                    ans_votes=qs_ans_votes,
                    ans_helpful=qs_ans_helpful_votes)

    class Meta(object):
        cache = SimpleCache()
        resource_name = 'kpi_vote'
        allowed_methods = ['get']


class FastResponseResource(CachedResource):
    """
    Returns the total number and number of Questions that receive an answer
    within a period of time.
    """
    date = fields.DateField('date')
    questions = fields.IntegerField('questions', default=0)
    responded = fields.IntegerField('responded', default=0)

    def get_object_list(self, request):
        qs = _qs_for(Question)

        # All answers tht were created within 3 days of the question
        aq = Answer.objects.filter(
                created__lt=F('question__created') + timedelta(days=3))
        # Qustions of said answers. This way results in a single query
        rs = qs.filter(id__in=aq.values_list('question'))

        # Merge and return
        return merge_results(responded=rs, questions=qs)

    class Meta:
        cache = SimpleCache()
        resource_name = 'kpi_fast_response'
        allowed_methods = ['get']


class ActiveKbContributorsResource(CachedResource):
    """
    Returns the number of active contributors in the KB.

    Returns en-US and non-en-US numbers separately.
    """
    date = fields.DateField('date')
    en_us = fields.IntegerField('en_us', default=0)
    non_en_us = fields.IntegerField('non_en_us', default=0)

    def get_object_list(self, request):
        # TODO: This whole method is yucky... Is there a nicer way to do this?
        # It will probably get soon nuked in favor of using the Metric model
        # when we need to go more granular than monthly.
        revisions = _monthly_qs_for(Revision)

        creators = revisions.values('year', 'month', 'creator').distinct()
        reviewers = revisions.values('year', 'month', 'reviewer').distinct()

        def _add_user(monthly_dict, year, month, userid):
            if userid:
                yearmonth = (year, month)
                if yearmonth not in monthly_dict:
                    monthly_dict[yearmonth] = set()
                monthly_dict[yearmonth].add(userid)

        def _add_users(monthly_dict, values, column):
            for r in values:
                _add_user(monthly_dict, r['year'], r['month'], r[column])

        # Build the en-US contributors list
        d = {}
        _add_users(d, creators.filter(document__locale='en-US'), 'creator')
        _add_users(d, reviewers.filter(document__locale='en-US'), 'reviewer')
        en_us_list = [{'month': k[1], 'year': k[0], 'count': len(v)} for
                      k, v in d.items()]

        # Build the non en-US contributors list
        d = {}
        _add_users(d, creators.exclude(document__locale='en-US'), 'creator')
        _add_users(d, reviewers.exclude(document__locale='en-US'), 'reviewer')
        non_en_us_list = [{'month': k[1], 'year': k[0], 'count': len(v)} for
                          k, v in d.items()]

        # Merge and return
        return merge_results(en_us=en_us_list, non_en_us=non_en_us_list)

    class Meta:
        cache = SimpleCache()
        resource_name = 'kpi_active_kb_contributors'
        allowed_methods = ['get']


class ActiveAnswerersResource(CachedResource):
    """
    Returns the number of active contributors in the support forum.

    Definition of contribution: wrote 10+ posts
    """
    date = fields.DateField('date')
    contributors = fields.IntegerField('contributors', default=0)

    def get_object_list(self, request):
        qs = _monthly_qs_for(Answer).values('year', 'month', 'creator')
        qs = qs.annotate(count=Count('creator'))
        answerers = qs.filter(count__gte=10)

        def _add_user(monthly_dict, year, month, userid):
            if userid:
                yearmonth = (year, month)
                if yearmonth not in monthly_dict:
                    monthly_dict[yearmonth] = set()
                monthly_dict[yearmonth].add(userid)

        # Build the answerers count list aggregated by month
        d = {}
        for a in answerers:
            _add_user(d, a['year'], a['month'], a['creator'])
        contributors = [{'month': k[1], 'year': k[0], 'count': len(v)} for
                        k, v in d.items()]

        # Merge and return
        return merge_results(contributors=contributors)

    class Meta:
        cache = SimpleCache()
        resource_name = 'kpi_active_answerers'
        allowed_methods = ['get']


def _monthly_qs_for(model_cls):
    """Return a queryset witht he extra select for month and year."""
    return model_cls.objects.filter(created__gte=_start_date()).extra(
        select={
            'month': 'extract( month from created )',
            'year': 'extract( year from created )',
        })


def _qs_for(model_cls):
    """Return the grouped queryset we need for model_cls."""
    return _monthly_qs_for(model_cls).values(
        'year', 'month').annotate(count=Count('created'))


def _start_date():
    """The date from which we start querying data."""
    # Lets start on the first day of the month a year ago
    year_ago = date.today() - timedelta(days=365)
    return date(year_ago.year, year_ago.month, 1)


def _remap_date_counts(**kwargs):
    """Remap the query result.
    kwargs = {<label>=[{'count': 45, 'month': 2L, 'year': 2010L},
     {'count': 12, 'month': 1L, 'year': 2010L},
      {'count': 1, 'month': 12L, 'year': 2009L},..],
      <label>=[{...},...],
      ...}
    returns
        [{
            datetime.date(2009, 12, 1): {'<label>': 1},
            datetime.date(2010, 1, 1): {'<label>': 12},
            datetime.date(2010, 2, 1): {'<label>': 45}
            ...
        },
        ...]
    """
    for label, qs in kwargs.iteritems():
        yield dict((date(x['year'], x['month'], 1), {label: x['count']})
                    for x in qs)


def merge_results(**kwargs):
    res_dict = reduce(_merge_results, _remap_date_counts(**kwargs))
    res_list = [dict(date=k, **v) for k, v in res_dict.items()]
    return [Struct(**x)
            for x in sorted(res_list, key=itemgetter('date'), reverse=True)]


def _merge_results(x, y):
    """Merge query results arrays into one array.

    From:
        [{"date": "2011-10-01", "votes": 3},...]
        and
        [{"date": "2011-10-01", "helpful": 7},...]
    To:
        [{"date": "2011-10-01", "votes": 3, "helpful": 7},...]
    """
    return dict((s, dict(x.get(s, {}).items() + y.get(s, {}).items()))
                    for s in set(x.keys() + y.keys()))


def _cursor():
    """Return a DB cursor for reading."""
    return connections[router.db_for_read(Metric)].cursor()


def _parse_date(text):
    """Parse a text date like ``"2004-08-30`` into a triple of numbers.

    May fling ValueErrors or TypeErrors around if the input or date is invalid.
    It should at least be a string--I mean, come on.

    """
    return tuple(int(i) for i in text.split('-'))
