import logging
from threading import Thread, Timer
from time import sleep

from .constants import DPG_COMMAND_HANDLE, REFERENCE_OUTPUT_HANDLE, \
    MOVE_TO_HANDLE
from .connection import BTLEConnection
from .desk_position import DeskPosition
from .dpg_command import DPGCommand, DeskOffsetCommand, MemorySetting2Command
from .height_speed import HeightSpeed

_LOGGER = logging.getLogger(__name__)

NAME_HANDLE = 0x0003

PROP_GET_CAPABILITIES = 0x80
PROP_DESK_OFFSET = 0x81
PROP_USER_ID = 0x86
PROP_MEMORY_POSITION_1 = 0x0089
PROP_MEMORY_POSITION_2 = 0x008a

MOVE_TIMER_TIMEOUT = 30

HEIGHT_MIN = 0
HEIGHT_MAX = 64


class DPGCommandReadError(Exception):
    pass


class WrongFavoriteNumber(Exception):
    pass


class LinakDesk:

    def __init__(self, bdaddr):
        self._handlers = {
            REFERENCE_OUTPUT_HANDLE: self._handle_reference_notification,
            DPG_COMMAND_HANDLE: self._handle_dpg_notification,
        }

        self._bdaddr = bdaddr
        self._conn = BTLEConnection(bdaddr, self._handlers)

        self._name = None
        self._desk_offset = None
        self._fav_position_1 = None
        self._fav_position_2 = None
        self._height_speed = None

        self._target = None
        self._running = False
        self._manual_height_change = False
        self._stop_timer = None

    @property
    def name(self):
        return self._wait_for_variable('_name')

    @property
    def desk_offset(self):
        return self._wait_for_variable('_desk_offset')

    @property
    def favorite_position_1(self):
        return self._wait_for_variable('_fav_position_1')

    @property
    def favorite_position_2(self):
        return self._wait_for_variable('_fav_position_2')

    @property
    def current_height(self):
        if not self._running:
            self._query_height_speed()
        return self.height_speed.height

    @property
    def current_height_with_offset(self):
        return self._with_desk_offset(self.height_speed.height)

    @property
    def height_speed(self):
        return self._wait_for_variable('_height_speed')

    @property
    def is_running(self):
        return self._running

    @property
    def target_height(self):
        if not self._running:
            return None, None

        direction_up = self._target.raw > self.current_height.raw

        if not self._manual_height_change:
            return self._target.cm, direction_up
        else:
            # height is being changed manually, so no target height
            return None, direction_up

    def _query_initial_data(self):
        with self._conn as conn:
            self._name = conn.read_characteristic(NAME_HANDLE)
            conn.dpg_command(PROP_USER_ID)
            conn.dpg_command(PROP_GET_CAPABILITIES)

    def _query_desk_offset(self):
        with self._conn as conn:
            conn.dpg_command(PROP_DESK_OFFSET)

    def _query_memory_positions(self):
        with self._conn as conn:
            self._fav_position_1 = None
            self._fav_position_2 = None

            conn.dpg_command(PROP_MEMORY_POSITION_1)
            self._wait_for_variable('_fav_position_1')

            conn.dpg_command(PROP_MEMORY_POSITION_2)
            self._wait_for_variable('_fav_position_2')

    def _query_height_speed(self):
        with self._conn as conn:
            self._height_speed = HeightSpeed.from_bytes(
                conn.read_characteristic(REFERENCE_OUTPUT_HANDLE))

    def init(self):
        _LOGGER.debug("Querying the device..")

        """ We need to query for name before doing anything, without it device doesnt respond """
        self._query_initial_data()
        self._query_desk_offset()
        self._query_memory_positions()
        self._query_height_speed()

    def __str__(self):
        return "[%s] Desk offset: %s, name: %s\nFav position1: %s, Fav position 2: %s Height with offset: %s" % (
            self._bdaddr,
            self.desk_offset.human_cm,
            self.name,
            self._with_desk_offset(self.favorite_position_1).human_cm,
            self._with_desk_offset(self.favorite_position_2).human_cm,
            self._with_desk_offset(self.height_speed.height).human_cm,
        )

    def __repr__(self):
        return self.__str__()

    def move_to_cm(self, cm):
        calculated_raw = DeskPosition.raw_from_cm(cm - self._desk_offset.cm)
        self._move_to_raw(calculated_raw)

    def move_down(self):
        self._manual_height_change = True
        self.move_to_cm(self._desk_offset.cm + HEIGHT_MIN)

    def move_up(self):
        self._manual_height_change = True
        self.move_to_cm(self._desk_offset.cm + HEIGHT_MAX)

    def stop_movement(self):
        _LOGGER.debug("Move stopped")
        # send stop move
        self._running = False
        self._manual_height_change = False
        self._stop_timer.cancel()

    def move_to_fav(self, fav):
        if fav == 1:
            raw = self.favorite_position_1.raw
        elif fav == 2:
            raw = self.favorite_position_2.raw
        else:
            raise DPGCommandReadError('Favorite with position: %d does not exists' % fav)

        self._move_to_raw(raw)

    def _wait_for_variable(self, var_name):
        value = getattr(self, var_name)
        if value is not None:
            return value

        for _ in range(0, 100):
            value = getattr(self, var_name)

            if value is not None:
                return value

            sleep(0.2)

        raise DPGCommandReadError('Cannot fetch value for %s' % var_name)

    def _with_desk_offset(self, value):
        return DeskPosition(value.raw + self.desk_offset.raw)

    def _handle_dpg_notification(self, data):
        """Handle Callback from a Bluetooth (GATT) request."""
        _LOGGER.debug("Received notification from the device..")

        if data[0] != 0x1:
            raise DPGCommandReadError('DPG_Control packets needs to have 0x01 in first byte')

        command = DPGCommand.build_command(data)
        _LOGGER.debug("Received %s (%s)", command.__class__.__name__, command.decoded_value)

        if command.__class__ == DeskOffsetCommand:
            self._desk_offset = command.offset
        elif command.__class__ == MemorySetting2Command:
            # DPG1M replies to queries for both memory positions with the same
            # command type (0x07)
            if self._fav_position_1 is None:
                self._fav_position_1 = command.offset
            else:
                self._fav_position_2 = command.offset

    def _handle_reference_notification(self, data):
        self._height_speed = HeightSpeed.from_bytes(data)

        _LOGGER.debug("Current relative height: %s, speed: %f", self._height_speed.height.human_cm, self._height_speed.speed.parsed)

    def _send_move_to(self):
        with self._conn as conn:
            _LOGGER.debug("Sending move to: %s", self._target.human_cm)
            conn.make_request(MOVE_TO_HANDLE, self._target.bytes, timeout=None)

    def _move_to_raw(self, raw_value):
        if self._running:
            self.stop_movement()

        current_raw_height = self.height_speed.height.raw
        if abs(raw_value - current_raw_height) < 10:
            _LOGGER.debug("Move not possible, current raw height: %d", current_raw_height)
            return

        self._target = DeskPosition(raw_value)
        self._running = True

        self._stop_timer = Timer(MOVE_TIMER_TIMEOUT, self.stop_movement)
        self._stop_timer.start()

        _LOGGER.debug("Start move to: %s", self._target.human_cm)

        Thread(target=self._process_movement).start()

    def _process_movement(self):
        while self._running:
            self._send_move_to()
            sleep(0.2)

            self._query_height_speed()
            if self.height_speed.speed.parsed < 0.001:
                self.stop_movement()
