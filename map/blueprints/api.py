import csv
import time
from collections import defaultdict

from flask import Blueprint, jsonify, make_response, request

from flask_login import current_user, login_required

from ldappool import ConnectionManager

from map import flask_redis

from .ldaptools import LDAPTools

bp = Blueprint('api', __name__, url_prefix='/api')
bp.config.from_object('config')

ldap = LDAPTools(
    ConnectionManager(bp.config["LDAP_SERVER"])
)


class APIError(Exception):
    status_code = 401

    def __init__(self, message, status_code=None, payload=None):
        Exception.__init__(self)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.payload = payload

    def to_dict(self):
        rv = dict(self.payload or ())
        rv['message'] = self.message
        return rv


@bp.errorhandler(APIError)
def handle_invalid_usage(error):
    response = jsonify(error.to_dict())
    response.status_code = error.status_code
    return response


def map_routine(self, which_room):
    room = self.flask_redis.hgetall(str(which_room))
    room_machines = self.flask_redis.lrange(room['key'] + "-machines", 0, -1)
    machines = {m: self.flask_redis.hgetall(m) for m in room_machines}
    num_rows = max([int(machines[m]['row']) for m in machines])
    num_cols = max([int(machines[m]['col']) for m in machines])

    num_machines = len(machines.keys())
    num_used = 0

    rows = []
    uuns = []
    for r in range(0, num_rows+1):
        unsorted_cells = []
        for c in range(0, num_cols+1):
            default_cell = {'hostname': None, 'col': c, 'row': r}
            cell = [v for (k, v) in machines.items()
                    if int(v['row']) == r and int(v['col']) == c]
            if not cell:
                cell = default_cell
            else:
                cell = cell[0]

            try:
                if cell['user'] is not "" or cell['status'] == "offline":
                    num_used += 1
            except Exception:
                pass

            if 'user' in cell:
                if cell['user'] == "":
                    del cell['user']
                else:
                    uun = current_user.get_friend(cell['user'])
                    if uun:
                        uuns.append(uun)
                        cell["user"] = uun
                    else:
                        cell["user"] = "-"

            unsorted_cells.append(cell)

        cells = unsorted_cells
        rows.append(cells)

    uun_names = self.ldap.get_names(uuns)

    for y in range(len(rows)):
        for x in range(len(rows[y])):
            cell = rows[y][x]
            if "user" in cell:
                uun = cell["user"]
                if uun in uun_names:
                    rows[y][x]["friend"] = uun_names[uun]

    num_free = num_machines - num_used

    low_availability = num_free <= 0.3 * num_machines

    last_update = float(self.flask_redis.get("last-update"))

    # Annotate friends with "here" if they are here
    room_key = room['key']
    friends = self.get_friend_rooms()
    friends_here, friends_elsewhere = (0, 0)
    for i in range(len(friends)):
        if friends[i]['room_key'] == room_key:
            friends[i]['here'] = True
            friends_here += 1
        else:
            friends_elsewhere += 1

    return {
        "friends": friends,
        "friends_here_count": friends_here,
        "friends_elsewhere_count": friends_elsewhere,
        "room": room,
        "rows": rows,
        "num_free": num_free,
        "num_machines": num_machines,
        "low_availability": low_availability,
        "last_update": last_update
    }


def rooms_list():
    rooms = list(flask_redis.smembers("forresthill-rooms"))
    rooms.sort()
    for i in range(len(rooms)):
        room = rooms[i]
        rooms[i] = (room, flask_redis.hget(room, "name"))

    return rooms


def room_machines(which):
    machines = flask_redis.lrange(which + "-machines", 0, -1)
    return machines


def get_friends():
    friends = flask_redis.smembers(current_user.get_username() + "-friends")
    friends = list(friends)

    with ldap.conn() as ldap_conn:
        friend_names = ldap.get_names_bare(friends, ldap_conn)

        for i in range(len(friends)):
            uun = friends[i]
            friend = uun

            if uun in friend_names:
                friend = friend_names[uun]

            friends[i] = (friend, uun)
    return friends


def get_friend_rooms():
    rooms = map(lambda name: flask_redis.hgetall(name),
                flask_redis.smembers("forresthill-rooms"))
    rooms = sorted(rooms, key=lambda x: x['key'])

    friends_rooms = set()
    if current_user.is_authenticated:
        for room in rooms:
            room_machines = flask_redis.lrange(room['key'] + "-machines", 0, -1)
            for machineName in room_machines:
                machine = flask_redis.hgetall(machineName)
                if current_user.has_friend(machine['user']):
                    uun = current_user.get_friend(machine['user'])
                    friends_rooms.add((uun, room['key'], room['name']))
        friends_rooms = list(friends_rooms)

        # uun -> name
        names = ldap.get_names([f[0] for f in friends_rooms])
        for i in range(len(friends_rooms)):
            uun, b, c = friends_rooms[i]
            if uun in names:
                friends_rooms[i] = {
                    'uun': uun,
                    'name': names[uun],
                    'room_key': b,
                    'room_name': c
                }

        friends_rooms.sort(key=lambda x: x['name'])

    return friends_rooms


# WARNING!!!! THIS METHOD IS UNAUTHENTICATED!!!!!
@bp.route('/refresh')
def refresh_data():
    """
    Returns a new update
    """
    # Doesn't seem to be used anywhere
    # default = "drillhall"
    which = request.args.get('site', '')

    # SENSITIVE CODE!!!!
    # THIS IS_ANONYMOUS CHECK IS WHAT GUARDS
    # AGAINST NON-LOGGED IN ACCESS
    is_demo = True
    if current_user.is_anonymous or which == "":
        this = get_demo_json()
    else:
        try:
            this = map_routine(which)
            is_demo = False
        except KeyError:
            this = get_demo_json()

        resp = make_response(jsonify(this))
        if not is_demo:
            resp.cache_control.max_age = 60

        return resp


def get_demo_friends():
    return [
        {
            'name': 'gryffindor',
            'room_key': 'godric\'s-hollow',
            'room_name': 'Godric\'s Hollow',
        },
        {
            'name': 'moony',
            'room_key': 'common-room',
            'room_name': 'Common Room',
            'here': True,
        },
        {
            'name': 'padfoot',
            'room_key': 'common-room',
            'room_name': 'Common Room',
            'here': True,
        },
        {
            'name': 'prongs',
            'room_key': 'common-room',
            'room_name': 'Common Room',
            'here': True,
        },
        {
            'name': 'wormtail',
            'room_key': 'common-room',
            'room_name': 'Common Room',
            'here': True,
        },
    ]


def get_demo_json():
    return {
        'friends': get_demo_friends(),
        'friends_here_count': 4,
        'friends_elsewhere_count': 1,
        'room': {"name": "Mapp Demo", "key": "demo"},
        'rows': [
            [
                {},
                {"hostname": "dish", "status": "offline"},
                {"hostname": "paulajennings", "status": "online"},
                {}, {}, {}, {}, {}
            ], [
                {"hostname": "dent", "status": "online"},
                {"hostname": "prefect", "status": "online"},
                {"hostname": "slartibartfast", "user": " ", "friend": "moony"},
                {"hostname": "random", "user": " ", "friend": "wormtail"},
                {"hostname": "colin", "status": "offline"},
                {},
                {"hostname": "marvin", "status": "online"},
                {"hostname": "vogon", "status": "online"}
            ], [
                {"hostname": "beeblebrox", "user": " ", "friend": "padfoot"},
                {"hostname": "trillian", "user": " "},
                {"hostname": "agrajag", "status": "unknown"},
                {"hostname": "krikkit", "user": " "},
                {"hostname": "almightybob", "status": "online"},
                {},
                {"hostname": "jynnan", "user": " "},
                {"hostname": "tonyx", "status": "offline"}
            ], [
                {}, {}, {},
                {"hostname": "eddie", "status": "offline"},
                {"hostname": "fenchurch", "user": " "},
                {}, {}, {}
            ], [
                {}, {}, {},
                {"hostname": "anangus", "status": "offline"},
                {"hostname": "benjy", "user": " ", "friend": "prongs"},
                {}, {}, {}
            ]
        ],
        'num_machines': 20,
        'num_free': 6,
        'low_availability': False,
        'last_update': "1998-05-02 13:37"
    }


@bp.route("/rooms")
@bp.route("/rooms/<which>")
def rooms(which=""):
    if not which:
        return jsonify({'rooms': rooms_list()})
    else:
        if which == "all":
            which = ",".join([r[0] for r in rooms_list()])
        machines = []
        for room in which.split(","):
            machines.extend(room_machines(room))
        return jsonify({"machines": machines})


@bp.route('/update_schema', methods=['POST'])
def update_schema():
    content = request.json

    try:
        key = content['callback-key']
    except Exception:
        key = ""

    if key not in flask_redis.lrange("authorised-key", 0, -1):
        # HTTP 401 Not Authorised
        print("******* CLIENT ATTEMPTED TO USE BAD KEY *******")
        raise APIError("Given key is not an authorised API key")

    try:
        sheetInput = content['machines']
    except Exception:
        raise APIError("no machines?")

    try:
        resetAll = content['resetAll']
    except Exception:
        raise APIError("Expected resetAll key")

    try:
        dropOnly = content['dropOnly']
    except Exception:
        raise APIError("Expected dropOnly key")

    roomKeys = ['site', 'key', 'name']

    pipe = flask_redis.pipeline()
    sites = defaultdict(list)

    for sheet in sheetInput:
        preader = csv.reader(sheet['csv'].split('\r\n'), delimiter=',')

        room = {}
        machines = []

        for rownumber, row in enumerate(preader):
            for colnumber, cell_value in enumerate(row):
                # handle header rows first
                if rownumber == 0:
                    if colnumber < len(roomKeys):
                        expected = roomKeys[colnumber]
                        if expected != cell_value:
                            raise APIError(("[Sheet {}] Invalid header '{}' in cols[{}],"
                                            " expected '{}'").format(sheet['name'], cell_value,
                                                                     colnumber, expected))
                    continue
                elif rownumber == 1:
                    if colnumber >= len(roomKeys):
                        continue
                    if cell_value == "":
                        raise APIError(("[Sheet {}] Invalid value in col[{}] row[{}], "
                                        "expected non-empty string").format(sheet['name'],
                                                                            colnumber,
                                                                            rownumber))
                    colName = roomKeys[colnumber]
                    room[colName] = cell_value
                    continue
                elif rownumber == 2:
                    if cell_value != "":
                        raise APIError(("[Sheet {}] Invalid value '{}' in rows[{}], "
                                        "expected empty row").format(sheet['name'], cell_value,
                                                                     rownumber))
                    continue

                hostname = cell_value

                if hostname != "" and not dropOnly:
                    machines.append({
                        'hostname': hostname,
                        'col': colnumber,
                        'row': rownumber-3,  # -3 required because first 3 rows are headers
                        'user': '',
                        'timestamp': '',
                        'status': 'offline',
                        'site': room['site'],
                        'room': room['key'],
                    })

        # if we aren't resetting the entire schema, reset just this room first
        if not resetAll:
            schema_reset_room(pipe, room['site'], room['key'], dropFromSite=True)

        # if dropping only, continue
        if dropOnly:
            continue

        # add site to list of sites
        pipe.sadd('mapp.sites', room['site'])
        # add room to site
        pipe.sadd(room['site']+'-rooms', room['key'])
        # add room dict
        pipe.hmset(room['key'], room)
        # add room machine listing
        pipe.lpush(room['key'] + '-machines', *map(lambda m: m['hostname'], machines))
        # add each machine
        for m in machines:
            pipe.hmset(m['hostname'], m)

        sites[room['site']].append(room)

    if resetAll:
        schema_reset(site="forresthill")

    pipe.execute()

    return jsonify({'success': True})


def schema_reset(site):
    """
    Completely resets the schema for a site.

    - list site rooms
        - list room machines
            - remove machine entries
        - remove room entry
    - remove site entry
    """
    siteKey = site + "-rooms"
    pipe = flask_redis.pipeline()

    for room in flask_redis.smembers(siteKey):
        schema_reset_room(pipe, site, room)

    pipe.delete(site)
    pipe.srem("mapp.sites", site)

    pipe.execute()


def schema_reset_room(pipe, site, room, dropFromSite=False):
    roomKey = room + "-machines"
    machines = flask_redis.lrange(roomKey, 0, -1)
    if len(machines) > 0:
        pipe.delete(*machines)
    pipe.delete(roomKey)
    pipe.delete(room)

    if dropFromSite:
        pipe.srem(site+'-rooms', room)


@bp.route('/update', methods=['POST'])
def update():
    content = request.json

    try:
        key = content['callback-key']
    except Exception:
        key = ""

    if key not in flask_redis.lrange("authorised-key", 0, -1):
        # HTTP 401 Not Authorised
        print("******* CLIENT ATTEMPTED TO USE BAD KEY *******")
        raise APIError("Given key is not an authorised API key")

    pipe = flask_redis.pipeline()

    try:
        for machine in content['machines']:
            host = machine['hostname']
            user = machine['user']
            ts = machine['timestamp']
            status = machine['status']

            pipe.hset(host, "user", user)
            pipe.hset(host, "timestamp", ts)
            pipe.hset(host, "status", status)

    except Exception:
        print("Malformed JSON content")
        raise APIError("Malformed JSON content", status_code=400)

    pipe.set("last-update", time.time())
    pipe.execute()

    return jsonify(status="ok")


@bp.route("/update_available", methods=['POST'])
@login_required
def update_available():
    content = request.json

    try:
        client_time = float(content['timestamp'])
    except Exception:
        raise APIError("Malformed JSON POST data", status_code=400)

    last_update = float(flask_redis.get("last-update"))
    user_behind = client_time < last_update

    return jsonify(status=str(user_behind))


@bp.route("/friends", methods=['GET', 'POST'])
@login_required
def friends():
    if request.method == "POST":
        formtype = request.form.get('type')
        if formtype == "del":
            remove_friends = request.form.getlist('delfriends[]')
            flask_redis.srem(current_user.get_username() + "-friends", *remove_friends)
        elif formtype == "add":
            add_friend = request.form.get('uun')

            # if(re.match("^[A-Za-z]+\ [A-Za-z]+$", add_friend) == None):
            #     raise APIError("Friend name expected in [A-z]+\ [A-z]+ form.",
            #                    status_code=400)
            flask_redis.sadd(current_user.get_username() + "-friends", add_friend)

    friends = get_friends()
    friends = map(lambda p: ("%s (%s)" % (p[0], p[1]), p[1]), friends)

    friends = sorted(friends, key=lambda s: s[0].lower())

    return jsonify(friendList=friends)  # Set up for ajax responses


@bp.route("/search", methods=['GET'])
@login_required
def search_friends():
    name = request.args.get('name', '')
    if len(name) < 2:
        return jsonify(people=[])

    people = sorted(ldap.search_name(name), key=lambda p: p['name'].lower())
    friends = get_friends()

    for person in people:
        if (person['name'], person['uun']) in friends:
            person['friend'] = True

    return jsonify(people=people)  # Set up for ajax responses
