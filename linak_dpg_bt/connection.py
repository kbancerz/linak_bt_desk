"""
Taken from python-eq3bt/master/eq3bt/connection.py

A simple wrapper for bluepy's btle.Connection.
Handles Connection duties (reconnecting etc.) transparently.
"""
import logging
import codecs

import struct
from bluepy import btle

from time import sleep
from .dpg_command import DPGCommand

DEFAULT_TIMEOUT = 1

_LOGGER = logging.getLogger(__name__)

DPG_COMMAND_HANDLE = 0x0014


class BTLEConnection(btle.DefaultDelegate):
    """Representation of a BTLE Connection."""

    def __init__(self, mac, callbacks):
        """Initialize the connection."""
        btle.DefaultDelegate.__init__(self)

        self._conn = None
        self._mac = mac
        self._callbacks = callbacks

    def __enter__(self):
        """
        Context manager __enter__ for connecting the device
        :rtype: btle.Peripheral
        :return:
        """
        if self._conn is None:
            self._conn = btle.Peripheral()
            self._conn.withDelegate(self)
            _LOGGER.debug("Trying to connect to %s", self._mac)
            try:
                self._conn.connect(self._mac, addrType='random')
            except btle.BTLEException as ex:
                _LOGGER.debug("Unable to connect to the device %s, retrying: %s", self._mac, ex)
                try:
                    self._conn.connect(self._mac, addrType='random')
                except Exception as ex2:
                    _LOGGER.error("Second connection try to %s failed: %s", self._mac, ex2)
                    raise

            _LOGGER.debug("Connected to %s", self._mac)

            for handle, callback in self._callbacks.items():
                self._subscribe_to_notification(handle, callback)
            _LOGGER.debug("Callbacks registered")

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.disconnect()

    def __del__(self):
        self.disconnect()

    def disconnect(self):
        if self._conn:
            self._conn.disconnect()
            self._conn = None

    def handleNotification(self, handle, data):
        """Handle Callback from a Bluetooth (GATT) request."""
        _LOGGER.debug("Got notification from %s: %s", hex(handle), codecs.encode(data, 'hex'))
        if handle in self._callbacks:
            self._callbacks[handle](data)

    @property
    def mac(self):
        """Return the MAC address of the connected device."""
        return self._mac

    def _set_callback(self, handle, function):
        """Set the callback for a Notification handle. It will be called with the parameter data, which is binary."""
        self._callbacks[handle] = function

    def _subscribe_to_notification(self, notification_resp_handle, callback):
        self.make_request(notification_resp_handle + 1, struct.pack('BB', 1, 0), with_response=True)
        self._set_callback(notification_resp_handle, callback)

    def make_request(self, handle, value, timeout=DEFAULT_TIMEOUT, with_response=True):
        """Write a GATT Command without callback - not utf-8."""
        try:
            _LOGGER.debug("Writing %s to %s with with_response=%s", codecs.encode(value, 'hex'), hex(handle), with_response)
            self._conn.writeCharacteristic(handle, value, withResponse=with_response)
            if timeout:
                _LOGGER.debug("Waiting for notifications for %s", timeout)
                self._conn.waitForNotifications(timeout)
        except btle.BTLEException as ex:
            _LOGGER.error("Got exception from bluepy while making a request: %s", ex)
            raise ex

    def read_characteristic(self, handle):
        """Read a GATT Characteristic."""
        try:
            _LOGGER.debug("Reading %s", hex(handle))
            return self._conn.readCharacteristic(handle)
        except btle.BTLEException as ex:
            _LOGGER.error("Got exception from bluepy while making a request: %s", ex)
            raise ex

    def dpg_command(self, command_type):
        value = DPGCommand.wrap_read_command(command_type)
        self.make_request(DPG_COMMAND_HANDLE, value)
        sleep(0.2)
