import hashlib
from uber.common import *


def dynamic(func):
    setattr(func, '_dynamic', True)
    return func


def create_namespace_uuid(s):
    return uuid.UUID(hashlib.sha1(s.encode('utf-8')).hexdigest()[:32])


class keydefaultdict(defaultdict):
    """
    Like a defaultdict except that the factory function used to generate values
    for missing keys takes the key as a parameter.
    """
    def __missing__(self, key):
        if self.default_factory is None:
            raise KeyError(key)
        else:
            ret = self[key] = self.default_factory(key)
            return ret


class _Overridable:
    "Base class we extend below to allow plugins to add/override config options."
    @classmethod
    def mixin(cls, klass):
        for attr in dir(klass):
            if not attr.startswith('_'):
                setattr(cls, attr, getattr(klass, attr))
        return cls

    def include_plugin_config(self, plugin_config):
        """Plugins call this method to merge their own config into the global c object."""

        for attr, val in plugin_config.items():
            if not isinstance(val, dict):
                setattr(self, attr.upper(), val)

        if 'enums' in plugin_config:
            self.make_enums(plugin_config['enums'])

        if 'dates' in plugin_config:
            self.make_dates(plugin_config['dates'])

    def make_dates(self, config_section):
        """
        Plugins can define a [dates] section in their config to create their
        own deadlines on the global c object.  This method is called automatically
        by c.include_plugin_config() if a "[dates]" section exists.
        """
        for _opt, _val in config_section.items():
            if not _val:
                _dt = None
            elif ' ' in _val:
                _dt = self.EVENT_TIMEZONE.localize(datetime.strptime(_val, '%Y-%m-%d %H'))
            else:
                _dt = self.EVENT_TIMEZONE.localize(datetime.strptime(_val + ' 23:59', '%Y-%m-%d %H:%M'))
            setattr(self, _opt.upper(), _dt)
            if _dt:
                self.DATES[_opt.upper()] = _dt

    def make_enums(self, config_section):
        """
        Plugins can define an [enums] section in their config to create their
        own enums on the global c object.  This method is called automatically
        by c.include_plugin_config() if an "[enums]" section exists.
        """
        for name, subsection in config_section.items():
            c.make_enum(name, subsection)

    def make_enum(self, enum_name, section, prices=False):
        """
        Plugins can call this to define individual enums, or call the make_enums
        function to make all enums defined there.  See the [enums] section in
        configspec.ini file, which explains what fields are added to the global
        c object for each enum.
        """
        opts, lookup, varnames = [], {}, []
        for name, desc in section.items():
            if isinstance(desc, int):
                val, desc = desc, name
            else:
                varnames.append(name.upper())
                val = self.create_enum_val(name)

            if desc:
                opts.append((val, desc))
                if prices:
                    lookup[desc] = val
                else:
                    lookup[val] = desc

        enum_name = enum_name.upper()
        setattr(self, enum_name + '_OPTS', opts)
        setattr(self, enum_name + '_VARS', varnames)
        setattr(self, enum_name + ('' if enum_name.endswith('S') else 'S'), lookup)

    def create_enum_val(self, name):
        val = int(sha512(name.upper().encode()).hexdigest()[:7], 16)
        setattr(self, name.upper(), val)
        return val


class Config(_Overridable):
    """
    We have two types of configuration.  One is the values which come directly from our config file, such
    as the name of our event.  The other is things which depend on the date/time (such as the badge price,
    which can change over time), or whether we've hit our configured attendance cap (which changes based
    on the state of the database).  See the comments in configspec.ini for explanations of the particilar
    options, which are documented there.

    This class has a single global instance called "c" which contains values of either type of config, e.g.
    if you need to check whether dealer registration is open in your code, you'd say c.DEALER_REG_OPEN
    For all of the datetime config options, we also define BEFORE_ and AFTER_ properties, e.g. you can
    check the booleans returned by c.BEFORE_PLACEHOLDER_DEADLINE or c.AFTER_PLACEHOLDER_DEADLINE
    """

    def get_oneday_price(self, dt):
        return self.BADGE_PRICES['single_day'].get(dt.strftime('%A'), self.DEFAULT_SINGLE_DAY)

    def get_presold_oneday_price(self, badge_type):
        return self.BADGE_PRICES['single_day'].get(self.BADGES[badge_type], self.DEFAULT_SINGLE_DAY)

    def get_attendee_price(self, dt=None):
        price = self.INITIAL_ATTENDEE
        if self.PRICE_BUMPS_ENABLED:

            for day, bumped_price in sorted(self.PRICE_BUMPS.items()):
                if (dt or sa.localized_now()) >= day:
                    price = bumped_price

            # Only check bucket-based pricing if we're not checking an existing badge AND
            # we don't have hardcore_optimizations_enabled config on AND we're not on-site
            # (because on-site pricing doesn't involve checking badges sold).
            if not dt and not self.HARDCORE_OPTIMIZATIONS_ENABLED and sa.localized_now() < c.EPOCH:
                badges_sold = self.BADGES_SOLD

                for badge_cap, bumped_price in sorted(self.PRICE_LIMITS.items()):
                    if badges_sold >= badge_cap and bumped_price > price:
                        price = bumped_price
        return price

    def get_group_price(self, dt=None):
        return self.get_attendee_price(dt) - self.GROUP_DISCOUNT

    def get_badge_count_by_type(self, badge_type):
        """
        Returns the count of all "Complete" badges of the given type; unlike the
        BADGES_SOLD property, this counts all paid values.  Thus we have counts
        for badge types that aren't typically sold, e.g. Staff badges.
        """
        with sa.Session() as session:
            return session.query(sa.Attendee).filter_by(badge_type=badge_type, badge_status=c.COMPLETED_STATUS).count()

    def get_printed_badge_deadline_by_type(self, badge_type):
        """
        Returns either PRINTED_BADGE_DEADLINE for custom badge types or the latter of PRINTED_BADGE_DEADLINE and
        SUPPORTER_BADGE_DEADLINE if the badge type is not preassigned (and only has a badge name if they're a supporter)
        """
        return c.PRINTED_BADGE_DEADLINE if badge_type in c.PREASSIGNED_BADGE_TYPES \
            else max(c.PRINTED_BADGE_DEADLINE, c.SUPPORTER_BADGE_DEADLINE)

    def after_printed_badge_deadline_by_type(self, badge_type):
        return sa.localized_now() > self.get_printed_badge_deadline_by_type(badge_type)

    @property
    def DEALER_REG_OPEN(self):
        return self.AFTER_DEALER_REG_START and self.BEFORE_DEALER_REG_SHUTDOWN

    @property
    @dynamic
    def DEALER_REG_SOFT_CLOSED(self):
        return self.AFTER_DEALER_REG_DEADLINE or self.DEALER_APPS >= self.MAX_DEALER_APPS \
            if self.MAX_DEALER_APPS and not self.HARDCORE_OPTIMIZATIONS_ENABLED else self.AFTER_DEALER_REG_DEADLINE

    @request_cached_property
    @dynamic
    def DEALER_APPS(self):
        with sa.Session() as session:
            return session.query(sa.Group).filter(sa.Group.tables > 0, sa.Group.cost > 0, sa.Group.status == self.UNAPPROVED).count()

    @request_cached_property
    @dynamic
    def BADGES_SOLD(self):
        with sa.Session() as session:
            attendees = session.query(sa.Attendee)
            individuals = attendees.filter(or_(sa.Attendee.paid == self.HAS_PAID, sa.Attendee.paid == self.REFUNDED)).filter(sa.Attendee.badge_status == self.COMPLETED_STATUS).count()
            group_badges = attendees.join(sa.Attendee.group).filter(sa.Attendee.paid == self.PAID_BY_GROUP,
                                                                    sa.Group.amount_paid > 0).count()
            return individuals + group_badges

    @request_cached_property
    @dynamic
    def BADGES_LEFT_AT_CURRENT_PRICE(self):
        """
        Returns a string representing a rough estimate of how many badges are left at the current badge price tier.
        """
        if c.HARDCORE_OPTIMIZATIONS_ENABLED:
            return None

        current_price_tier = c.ORDERED_PRICE_LIMITS.index(c.BADGE_PRICE) if c.BADGE_PRICE in c.ORDERED_PRICE_LIMITS else -1
        if current_price_tier != -1 and c.ORDERED_PRICE_LIMITS[current_price_tier] == c.ORDERED_PRICE_LIMITS[-1]\
                or not c.ORDERED_PRICE_LIMITS:
            if c.MAX_BADGE_SALES:
                difference = c.MAX_BADGE_SALES - c.BADGES_SOLD
            else:
                return -1
        else:
            for key, val in c.PRICE_LIMITS.items():
                if c.ORDERED_PRICE_LIMITS[current_price_tier+1] == val:
                    difference = key - c.BADGES_SOLD
        return difference

    @property
    @dynamic
    def ONEDAY_BADGE_PRICE(self):
        return self.get_oneday_price(sa.localized_now())

    @property
    @dynamic
    def BADGE_PRICE(self):
        return self.get_attendee_price()

    @property
    @dynamic
    def GROUP_PRICE(self):
        return self.get_group_price()

    @property
    def PREREG_BADGE_TYPES(self):
        types = [self.ATTENDEE_BADGE, self.PSEUDO_DEALER_BADGE]
        for reg_open, badge_type in [(self.BEFORE_GROUP_PREREG_TAKEDOWN, self.PSEUDO_GROUP_BADGE)]:
            if reg_open:
                types.append(badge_type)
        for badge_type in self.BADGE_TYPE_PRICES:
            if badge_type not in types:
                types.append(badge_type)
        return types

    @property
    @dynamic
    def PRESOLD_ONEDAY_BADGE_TYPES(self):
        return {
            badge_type: self.BADGES[badge_type]
            for badge_type, desc in self.AT_THE_DOOR_BADGE_OPTS
            if self.BADGES[badge_type] in c.DAYS_OF_WEEK
        }

    @property
    def PREREG_DONATION_OPTS(self):
        if self.BEFORE_SUPPORTER_DEADLINE and self.SUPPORTER_AVAILABLE:
            return self.DONATION_TIER_OPTS
        elif self.BEFORE_SHIRT_DEADLINE and self.SHIRT_AVAILABLE:
            return [(amt, desc) for amt, desc in self.DONATION_TIER_OPTS if amt < self.SUPPORTER_LEVEL]
        else:
            return [(amt, desc) for amt, desc in self.DONATION_TIER_OPTS if amt < self.SHIRT_LEVEL]

    @property
    def PREREG_DONATION_DESCRIPTIONS(self):
        # include only the items that are actually available for purchase
        if self.BEFORE_SUPPORTER_DEADLINE and self.SUPPORTER_AVAILABLE:
            donation_list = self.DONATION_TIER_DESCRIPTIONS.items()
        elif self.BEFORE_SHIRT_DEADLINE and self.SHIRT_AVAILABLE:
            donation_list = [tier for tier in c.DONATION_TIER_DESCRIPTIONS.items()
                             if tier[1]['price'] < self.SUPPORTER_LEVEL]
        else:
            donation_list = [tier for tier in c.DONATION_TIER_DESCRIPTIONS.items()
                             if tier[1]['price'] < self.SHIRT_LEVEL]

        donation_list = sorted(donation_list, key=lambda tier: tier[1]['price'])

        # add in all previous descriptions.  the higher tiers include all the lower tiers
        for entry in donation_list:
            all_desc_and_links = \
                [(tier[1]['description'], tier[1]['link']) for tier in donation_list
                    if tier[1]['price'] > 0 and tier[1]['price'] < entry[1]['price']] \
                + [(entry[1]['description'], entry[1]['link'])]

            # maybe slight hack. descriptions and links are separated by '|' characters so we can have multiple
            # items displayed in the donation tiers.  in an ideal world, these would already be separated in the INI
            # and we wouldn't have to do it here.
            entry[1]['all_descriptions'] = []
            for item in all_desc_and_links:
                descriptions = item[0].split('|')
                links = item[1].split('|')
                entry[1]['all_descriptions'] += list(zip(descriptions, links))

        return [dict(tier[1]) for tier in donation_list]

    @property
    def PREREG_DONATION_TIERS(self):
        return dict(self.PREREG_DONATION_OPTS)

    @property
    def PREREG_REQUEST_HOTEL_INFO_DEADLINE(self):
        """
        The datetime at which the "Request Hotel Info" checkbox will NO LONGER
        be shown during preregistration.
        """
        return self.PREREG_OPEN + timedelta(
            hours=max(0, self.PREREG_REQUEST_HOTEL_INFO_DURATION))

    @property
    def PREREG_REQUEST_HOTEL_INFO_ENABLED(self):
        """
        Boolean which indicates whether the "Request Hotel Info" checkbox is
        enabled generally, whether or not the deadline has passed.
        """
        return self.PREREG_REQUEST_HOTEL_INFO_DURATION > 0

    @property
    def PREREG_REQUEST_HOTEL_INFO_OPEN(self):
        """
        Boolean which indicates whether the "Request Hotel Info" checkbox is
        enabled and currently open with preregistration.
        """
        if not self.PREREG_REQUEST_HOTEL_INFO_ENABLED:
            return False
        return not self.AFTER_PREREG_REQUEST_HOTEL_INFO_DEADLINE

    @property
    def PREREG_HOTEL_INFO_EMAIL_DATE(self):
        """
        Date at which the hotel booking link email becomes available to send.
        """
        return self.PREREG_REQUEST_HOTEL_INFO_DEADLINE + \
            timedelta(hours=max(0, self.PREREG_HOTEL_INFO_EMAIL_WAIT_DURATION))

    @property
    @dynamic
    def AT_THE_DOOR_BADGE_OPTS(self):
        """
        This provides the dropdown on the /registration/register page with its
        list of badges available at-door.  It includes a "Full Weekend Pass"
        if attendee badges are available.  If one-days are enabled, it includes
        either a generic "Single Day Pass" or a list of specific day badges,
        based on the c.PRESELL_ONE_DAYS setting.
        """
        opts = []
        if self.ATTENDEE_BADGE_AVAILABLE:
            opts.append((self.ATTENDEE_BADGE, 'Full Weekend Pass (${})'.format(self.BADGE_PRICE)))
        for badge_type in self.BADGE_TYPE_PRICES:
            if badge_type not in opts:
                opts.append((badge_type, '{} (${})'.format(self.BADGES[badge_type], self.BADGE_TYPE_PRICES[badge_type])))
        if self.ONE_DAYS_ENABLED:
            if self.PRESELL_ONE_DAYS:
                day = max(sa.localized_now(), self.EPOCH)
                while day.date() <= self.ESCHATON.date():
                    day_name = day.strftime('%A')
                    price = self.BADGE_PRICES['single_day'].get(day_name) or self.DEFAULT_SINGLE_DAY
                    badge = getattr(self, day_name.upper())
                    if getattr(self, day_name.upper() + '_AVAILABLE', None):
                        opts.append((badge, day_name + ' Pass (${})'.format(price)))
                    day += timedelta(days=1)
            elif self.ONE_DAY_BADGE_AVAILABLE:
                opts.append((self.ONE_DAY_BADGE,  'Single Day Pass (${})'.format(self.ONEDAY_BADGE_PRICE)))
        return opts

    @property
    def PREREG_AGE_GROUP_OPTS(self):
        return [opt for opt in self.AGE_GROUP_OPTS if opt[0] != self.AGE_UNKNOWN]

    @property
    def AT_OR_POST_CON(self):
        return self.AT_THE_CON or self.POST_CON

    @property
    def PRE_CON(self):
        return not self.AT_OR_POST_CON

    @property
    def STAFF_GET_FOOD(self):
        """
        Certain events may run a complimentary consuite for staff and guests. Whether or not the consuite exists changes
        a lot of little things, like whether we collect food restrictions or describe free food as a benefit to
        staffing. To turn this on, add a department with the variable name `food_prep` to [[job_location]].
        Returns:
            Boolean: true if `food_prep` is defined in [[job_location]] config
        """
        return getattr(c, 'FOOD_PREP', None) in c.JOB_LOCATIONS

    @property
    def FINAL_EMAIL_DEADLINE(self):
        return min(c.UBER_TAKEDOWN, c.EPOCH)

    @property
    def CSRF_TOKEN(self):
        uber.utils.ensure_csrf_token_exists()
        return cherrypy.session['csrf_token'] if 'csrf_token' in cherrypy.session else ''

    @property
    def QUERY_STRING(self):
        return cherrypy.request.query_string

    @property
    def PAGE_PATH(self):
        return cherrypy.request.path_info

    @property
    def PAGE(self):
        return cherrypy.request.path_info.split('/')[-1]

    @request_cached_property
    @dynamic
    def ALLOWED_ACCESS_OPTS(self):
        with sa.Session() as session:
            return session.current_admin_account().allowed_access_opts

    @request_cached_property
    @dynamic
    def DISALLOWED_ACCESS_OPTS(self):
        return set(self.ACCESS_OPTS).difference(set(self.ALLOWED_ACCESS_OPTS))

    @request_cached_property
    @dynamic
    def CURRENT_ADMIN(self):
        try:
            with sa.Session() as session:
                attrs = sa.Attendee.to_dict_default_attrs + \
                    ['admin_account', 'assigned_depts']
                admin_account = session.query(sa.AdminAccount) \
                    .filter_by(id=cherrypy.session['account_id']).options(
                        subqueryload(sa.AdminAccount.attendee)
                            .subqueryload(sa.Attendee.assigned_depts)).one()
                return admin_account.attendee.to_dict(attrs)
        except Exception:
            return {}

    @request_cached_property
    @dynamic
    def DEPARTMENTS(self):
        return dict(self.DEPARTMENT_OPTS)

    @request_cached_property
    @dynamic
    def DEPARTMENT_OPTS(self):
        from uber.models.department import Department
        with sa.Session() as session:
            query = session.query(Department).order_by(Department.name)
            return [(d.id, d.name) for d in query]

    @request_cached_property
    @dynamic
    def DEPARTMENT_OPTS_WITH_DESC(self):
        from uber.models.department import Department
        with sa.Session() as session:
            query = session.query(Department).order_by(Department.name)
            return [(d.id, d.name, d.description) for d in query]

    @request_cached_property
    @dynamic
    def PUBLIC_DEPARTMENT_OPTS_WITH_DESC(self):
        from uber.models.department import Department
        with sa.Session() as session:
            query = session.query(Department).filter_by(
                solicits_volunteers=True).order_by(Department.name)
            return [('All', 'Anywhere', 'I want to help anywhere I can!')] + \
                [(d.id, d.name, d.description) for d in query]

    @request_cached_property
    @dynamic
    def DEFAULT_DEPARTMENT_ID(self):
        from uber.models.department import Department
        with sa.Session() as session:
            dept = session.query(Department).order_by(Department.name).first()
            return dept.id

    @property
    def HTTP_METHOD(self):
        return cherrypy.request.method.upper()

    def get_kickin_count(self, kickin_level):
        with sa.Session() as session:
            attendees = session.query(sa.Attendee)
            individual_supporters = attendees.filter(sa.Attendee.paid.in_([self.HAS_PAID, self.REFUNDED]),
                                                     sa.Attendee.amount_extra >= kickin_level).count()
            group_supporters = attendees.filter(sa.Attendee.paid == self.PAID_BY_GROUP,
                                                sa.Attendee.amount_extra >= kickin_level,
                                                sa.Attendee.amount_paid >= kickin_level).count()
            return individual_supporters + group_supporters

    @request_cached_property
    @dynamic
    def SUPPORTER_COUNT(self):
        return self.get_kickin_count(self.SUPPORTER_LEVEL)

    @request_cached_property
    @dynamic
    def SHIRT_COUNT(self):
        return self.get_kickin_count(self.SHIRT_LEVEL)

    @property
    @dynamic
    def REMAINING_BADGES(self):
        return max(0, self.MAX_BADGE_SALES - self.BADGES_SOLD)

    @request_cached_property
    @dynamic
    def MENU_FILTERED_BY_ACCESS_LEVELS(self):
        return c.MENU.render_items_filtered_by_current_access(sa.AdminAccount.access_set())

    @request_cached_property
    @dynamic
    def ADMIN_ACCESS_SET(self):
        return sa.AdminAccount.access_set()

    @request_cached_property
    @dynamic
    def EMAIL_APPROVED_IDENTS(self):
        with sa.Session() as session:
            return {ae.ident for ae in session.query(sa.ApprovedEmail)}

    @request_cached_property
    @dynamic
    def PREVIOUSLY_SENT_EMAILS(self):
        with sa.Session() as session:
            return set(session.query(sa.Email.model, sa.Email.fk_id, sa.Email.ident))

    def __getattr__(self, name):
        if name.split('_')[0] in ['BEFORE', 'AFTER']:
            date_setting = getattr(c, name.split('_', 1)[1])
            if not date_setting:
                return False
            elif name.startswith('BEFORE_'):
                return sa.localized_now() < date_setting
            else:
                return sa.localized_now() > date_setting
        elif name.startswith('HAS_') and name.endswith('_ACCESS'):
            return getattr(c, '_'.join(name.split('_')[1:-1])) in c.ADMIN_ACCESS_SET
        elif name.endswith('_COUNT'):
            item_check = name.rsplit('_', 1)[0]
            badge_type = getattr(self, item_check, None)
            return self.get_badge_count_by_type(badge_type) if badge_type else None
        elif name.endswith('_AVAILABLE'):
            item_check = name.rsplit('_', 1)[0]
            stock_setting = getattr(self, item_check + '_STOCK', None)
            if stock_setting is None:
                # Defaults to unlimited stock for any stock not configured
                return True

            # Only poll the DB if stock is configured
            count_check = getattr(self, item_check + '_COUNT', None)
            if count_check is None:
                # Things with no count are never considered available
                return False
            else:
                return int(count_check) < int(stock_setting)
        elif hasattr(_secret, name):
            return getattr(_secret, name)
        elif name.lower() in _config['secret']:
            return _config['secret'][name.lower()]
        else:
            raise AttributeError('no such attribute {}'.format(name))


class SecretConfig(_Overridable):
    """
    This class is for properties which we don't want to be used as Javascript
    variables.  Properties on this class can be accessed normally through the
    global c object as if they were defined there.
    """

    @property
    def SQLALCHEMY_URL(self):
        """
        support reading the DB connection info from an environment var (used with Docker containers)
        DB_CONNECTION_STRING should contain the full Postgres URI
        """
        db_connection_string = os.environ.get('DB_CONNECTION_STRING')

        if db_connection_string is not None:
            return db_connection_string
        else:
            return _config['secret']['sqlalchemy_url']

c = Config()
_secret = SecretConfig()

_config = parse_config(__file__)  # outside this module, we use the above c global instead of using this directly


def _unrepr(d):
    for opt in d:
        val = d[opt]
        if val in ['True', 'False']:
            d[opt] = ast.literal_eval(val)
        elif isinstance(val, str) and val.isdigit():
            d[opt] = int(val)
        elif isinstance(d[opt], dict):
            _unrepr(d[opt])

_unrepr(_config['appconf'])
c.APPCONF = _config['appconf'].dict()

c.BADGE_PRICES = _config['badge_prices']
for _opt, _val in chain(_config.items(), c.BADGE_PRICES.items()):
    if not isinstance(_val, dict) and not hasattr(c, _opt.upper()):
        setattr(c, _opt.upper(), _val)
for _opt, _val in c.BADGE_PRICES['stocks'].items():
    _opt = _opt.upper() + '_STOCK'
    if not hasattr(c, _opt):
        setattr(c, _opt, _val)

c.DATES = {}
c.TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'
c.DATE_FORMAT = '%Y-%m-%d'
c.EVENT_TIMEZONE = pytz.timezone(c.EVENT_TIMEZONE)
c.make_dates(_config['dates'])

c.PRICE_BUMPS = {}
c.PRICE_LIMITS = {}
for _opt, _val in c.BADGE_PRICES['attendee'].items():
    try:
        if ' ' in _opt:
            date = c.EVENT_TIMEZONE.localize(datetime.strptime(_opt, '%Y-%m-%d %H%M'))
        else:
            date = c.EVENT_TIMEZONE.localize(datetime.strptime(_opt, '%Y-%m-%d'))
    except ValueError:
        c.PRICE_LIMITS[int(_opt)] = _val
    else:
        c.PRICE_BUMPS[date] = _val
c.ORDERED_PRICE_LIMITS = sorted([val for key, val in c.PRICE_LIMITS.items()])


def _is_intstr(s):
    if s and s[0] in ('-', '+'):
        return s[1:].isdigit()
    return s.isdigit()


# Under certain conditions, we want to completely remove certain payment options from the system.
# However, doing so cleanly also risks an exception being raised if these options are referenced elsewhere in the code
# (i.e., c.STRIPE). So we create an enum val to allow code to check for these variables without exceptions.
if not c.KIOSK_CC_ENABLED:
    del _config['enums']['door_payment_method']['stripe']
    c.create_enum_val('stripe')

if not c.GROUPS_ENABLED:
    del _config['enums']['door_payment_method']['group']
    c.create_enum_val('group')

c.make_enums(_config['enums'])

_default_access = [getattr(c, s.upper()) for s in c.REQUIRED_ACCESS[c.__DEFAULT__]]
del c.REQUIRED_ACCESS[c.__DEFAULT__]
c.REQUIRED_ACCESS_VARS.remove('__DEFAULT__')
c.REQUIRED_ACCESS = keydefaultdict(lambda a: set([a] + _default_access),
    {a: set([getattr(c, s.upper()) for s in p]) for a, p in c.REQUIRED_ACCESS.items()})
c.REQUIRED_ACCESS_OPTS = [(a, c.REQUIRED_ACCESS[a]) for a, _ in c.REQUIRED_ACCESS_OPTS if a != c.__DEFAULT__]

for _name, _val in _config['integer_enums'].items():
    if isinstance(_val, int):
        setattr(c, _name.upper(), _val)

for _name, _section in _config['integer_enums'].items():
    if isinstance(_section, dict):
        _interpolated = OrderedDict()
        for _desc, _val in _section.items():
            if _is_intstr(_val):
                _price = int(_val)
            else:
                _price = getattr(c, _val.upper())

            _interpolated[_desc] = _price

        c.make_enum(_name, _interpolated, prices=_name.endswith('_price'))

c.BADGE_RANGES = {}
for _badge_type, _range in _config['badge_ranges'].items():
    c.BADGE_RANGES[getattr(c, _badge_type.upper())] = _range

c.BADGE_TYPE_PRICES = {}
for _badge_type, _price in _config['badge_type_prices'].items():
    try:
        c.BADGE_TYPE_PRICES[getattr(c, _badge_type.upper())] = _price
    except AttributeError:
        pass

c.make_enum('age_group', OrderedDict([(name, section['desc']) for name, section in _config['age_groups'].items()]))
c.AGE_GROUP_CONFIGS = {}
for _name, _section in _config['age_groups'].items():
    _val = getattr(c, _name.upper())
    c.AGE_GROUP_CONFIGS[_val] = dict(_section.dict(), val=_val)

c.TABLE_PRICES = defaultdict(lambda: _config['table_prices']['default_price'],
                             {int(k): v for k, v in _config['table_prices'].items() if k != 'default_price'})
c.PREREG_TABLE_OPTS = list(range(1, c.MAX_TABLES + 1))
c.ADMIN_TABLE_OPTS = [decimal.Decimal(x) for x in range(0, 9)]

c.SHIFTLESS_DEPTS = {getattr(c, dept.upper()) for dept in c.SHIFTLESS_DEPTS}
c.PREASSIGNED_BADGE_TYPES = [getattr(c, badge_type.upper()) for badge_type in c.PREASSIGNED_BADGE_TYPES]
c.TRANSFERABLE_BADGE_TYPES = [getattr(c, badge_type.upper()) for badge_type in c.TRANSFERABLE_BADGE_TYPES]

c.DEPT_HEAD_CHECKLIST = _config['dept_head_checklist']

c.CON_LENGTH = int((c.ESCHATON - c.EPOCH).total_seconds() // 3600)
c.START_TIME_OPTS = [(dt, dt.strftime('%I %p %a')) for dt in (c.EPOCH + timedelta(hours=i) for i in range(c.CON_LENGTH))]
c.DURATION_OPTS = [(i, '%i hour%s' % (i, ('s' if i > 1 else ''))) for i in range(1, 9)]
c.SETUP_TIME_OPTS = [
    (dt, dt.strftime('%I %p %a')) for dt in (c.EPOCH - timedelta(days=day) + timedelta(hours=hour)
        for day in range(c.SETUP_SHIFT_DAYS, 0, -1)
            for hour in range(24))]
c.TEARDOWN_TIME_OPTS = [
    (dt, dt.strftime('%I %p %a')) for dt in (c.ESCHATON + timedelta(days=day) + timedelta(hours=hour)
        for day in range(0, 2, 1)  # Allow two full days for teardown shifts
            for hour in range(24))]

# code for all time slots
c.CON_TOTAL_LENGTH = int((c.TEARDOWN_TIME_OPTS[-1][0] - c.SETUP_TIME_OPTS[0][0]).seconds / 3600)
c.ALL_TIME_OPTS = [(dt, dt.strftime('%I %p %a %d %b')) for dt in ((c.EPOCH - timedelta(days=c.SETUP_SHIFT_DAYS) + timedelta(hours=i)) for i in range(c.CON_TOTAL_LENGTH))]

c.EVENT_NAME_AND_YEAR = c.EVENT_NAME + (' {}'.format(c.YEAR) if c.YEAR else '')
c.EVENT_YEAR = c.EPOCH.strftime('%Y')
c.EVENT_MONTH = c.EPOCH.strftime('%B')
c.EVENT_START_DAY = int(c.EPOCH.strftime('%d')) % 100
c.EVENT_END_DAY = int(c.ESCHATON.strftime('%d')) % 100
c.SHIFTS_START_DAY = c.EPOCH - timedelta(days=c.SETUP_SHIFT_DAYS)

c.DAYS = sorted({(dt.strftime('%Y-%m-%d'), dt.strftime('%a')) for dt, desc in c.START_TIME_OPTS})
c.HOURS = ['{:02}'.format(i) for i in range(24)]
c.MINUTES = ['{:02}'.format(i) for i in range(60)]

c.DAYS_OF_WEEK = {'Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'}
if c.ONE_DAYS_ENABLED and c.PRESELL_ONE_DAYS:
    _day = c.EPOCH.date()
    while _day <= c.ESCHATON.date():
        _name = _day.strftime('%A')
        _val = c.create_enum_val(_name)
        c.BADGES[_val] = _name
        c.BADGE_OPTS.append((_val, _name))
        c.BADGE_VARS.append(_name.upper())
        c.BADGE_RANGES[_val] = c.BADGE_RANGES[c.ONE_DAY_BADGE]
        c.TRANSFERABLE_BADGE_TYPES.append(_val)
        _day += timedelta(days=1)

c.MAX_BADGE = max(xs[1] for xs in c.BADGE_RANGES.values())

c.JOB_LOCATION_OPTS.sort(key=lambda tup: tup[1])

c.JOB_PAGE_OPTS = (
    ('index',    'Calendar View'),
    ('signups',  'Signups View'),
    ('staffers', 'Staffer Summary')
)
c.WEIGHT_OPTS = (
    ('1.0', 'x1.0'),
    ('1.5', 'x1.5'),
    ('2.0', 'x2.0'),
    ('2.5', 'x2.5'),
)
c.JOB_DEFAULTS = ['name', 'description', 'duration', 'slots', 'weight', 'required_roles_ids', 'extra15']

c.PREREG_SHIRT_OPTS = c.SHIRT_OPTS[1:]
c.MERCH_SHIRT_OPTS = [(c.SIZE_UNKNOWN, 'select a size')] + list(c.PREREG_SHIRT_OPTS)
c.DONATION_TIER_OPTS = [(amt, '+ ${}: {}'.format(amt, desc) if amt else desc) for amt, desc in c.DONATION_TIER_OPTS]

c.DONATION_TIER_DESCRIPTIONS = _config.get('donation_tier_descriptions', {})
for tier in c.DONATION_TIER_DESCRIPTIONS.items():
    tier[1]['price'] = [amt for amt, name in c.DONATION_TIERS.items() if name == tier[1]['name']][0]

c.STORE_ITEM_NAMES = [desc for val, desc in c.STORE_PRICE_OPTS]
c.FEE_ITEM_NAMES = [desc for val, desc in c.FEE_PRICE_OPTS]

c.WRISTBAND_COLORS = defaultdict(lambda: c.WRISTBAND_COLORS[c.DEFAULT_WRISTBAND], c.WRISTBAND_COLORS)

c.SAME_NUMBER_REPEATED = r'^(\d)\1+$'
c.INVALID_BADGE_PRINTED_CHARS = r'[^a-zA-Z0-9!"#$%&\'()*+,\-\./:;<=>?@\[\\\]^_`\{|\}~ "]'  # Allows 0-9, a-z, A-Z, and a handful of punctuation characters
c.EVENT_QR_ID = c.EVENT_QR_ID or c.EVENT_NAME_AND_YEAR.replace(' ', '_').lower()

try:
    _items = sorted([int(step), url] for step, url in _config['volunteer_checklist'].items() if url)
except ValueError:
    log.error('[volunteer_checklist] config options must have integer option names')
    raise
else:
    c.VOLUNTEER_CHECKLIST = [url for step, url in _items]

stripe.api_key = c.STRIPE_SECRET_KEY

# plugins can use this to append paths which will be included as <script> tags, e.g. if a plugin
# appends '../static/foo.js' to this list, that adds <script src="../static/foo.js"></script> to
# all of the pages on the site except for preregistration pages (for performance)
c.JAVASCRIPT_INCLUDES = []
