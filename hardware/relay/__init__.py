# -*- coding: utf-8 -*-
import terrariumLogging
logger = terrariumLogging.logging.getLogger(__name__)

import threading
import inspect
import pkgutil
from importlib import import_module
import sys

from pathlib import Path
from hashlib import md5
from time import time, sleep
from operator import itemgetter

# pip install retry
from retry import retry

from terrariumUtils import terrariumUtils, terrariumCache, classproperty

class terrariumRelayException(TypeError):
  '''There is a problem with loading a hardware switch. Invalid power switch action.'''

  def __init__(self, message, *args):
    self.message = message
    super().__init__(message, *args)

class terrariumRelayLoadingException(terrariumRelayException):
  pass

class terrariumRelayUpdateException(terrariumRelayException):
  pass

class terrariumRelayActionException(terrariumRelayException):
  pass

# Factory class
class terrariumRelay(object):
  HARDWARE = None
  NAME = None

  OFF =   0.0
  ON  = 100.0

  @classproperty
  def available_hardware(__cls__):
    __CACHE_KEY = 'known_relays'
    cache = terrariumCache()

    data = cache.get_data(__CACHE_KEY)
    if data is None:
      data = {}
      # Start dynamically loading sensors (based on: https://www.bnmetrics.com/blog/dynamic-import-in-python3)
      for file in sorted(Path(__file__).parent.glob('*_relay.py')):
        imported_module = import_module( '.' + file.stem, package='{}'.format(__name__))

        for i in dir(imported_module):
          attribute = getattr(imported_module, i)

          if inspect.isclass(attribute) and attribute != __cls__ and issubclass(attribute, __cls__):
            setattr(sys.modules[__name__], file.stem, attribute)
            data[attribute.HARDWARE] = attribute

      cache.set_data(__CACHE_KEY,data,-1)

    return data

  @classproperty
  def available_relays(__cls__):
    data = []
    for (hardware_type, relay) in __cls__.available_hardware.items():
      if relay.NAME is not None:
        data.append({'hardware' : hardware_type, 'name' : relay.NAME})

    return sorted(data, key=itemgetter('name'))

  # Return polymorph relay....
  def __new__(cls, id, hardware_type, address, name = '', prev_state = None, callback = None):
    known_relays = terrariumRelay.available_hardware

    if hardware_type not in known_relays:
      raise terrariumRelayException(f'Relay of hardware type {hardware_type} is unknown.')

    return super(terrariumRelay, cls).__new__(known_relays[hardware_type])

  def __init__(self, id, _, address, name = '', prev_state = None, callback = None):
    self._device = {'device'      : None,
                    'address'     : None,
                    'name'        : None,
                    'switch'      : None,
                    'type'        : None,
                    'id'          : None,
                    'cache_key'   : None,
                    'callback'    : None,

                    'wattage'     : 0.0,
                    'flow'        : 0.0,
#                    'replacement' : '2019-01-01',
                    'manual_mode' : False,

                    'last_update' : 0,
                    'value'       : self.OFF}

    self.__relay_cache = terrariumCache()

    self.id = id
    self.name = name
    self.address = address
    self.callback = callback

    #self.timer = terrariumTimer('00:00','00:00',0,0,False)

    self.load_hardware()

#    self.update()

    # if self._device['value'] is None:
    #   logger.warning(f'Forcing power off for switch {self.name} as there no old state available')
    #   self.set_state(self.OFF, True)

  def __repr__(self):
    return f'{self.NAME} {self.type} named \'{self.name}\' at address \'{self.address}\''

  @retry(terrariumRelayLoadingException, tries=3, delay=0.5, max_delay=2, logger=logger)
  def load_hardware(self):
    hardware_cache_key = md5(f'HW-{self.HARDWARE}-{self.address}'.encode()).hexdigest()
    hardware = self.__relay_cache.get_data(hardware_cache_key)
    if hardware is None:
      try:
        hardware = self._load_hardware()

        if hardware is None:
          raise terrariumRelayLoadingException(f'Could not load hardware for relay {self}: Unknown error')

        self.__relay_cache.set_data(hardware_cache_key,hardware,-1)
      except Exception as ex:
        raise terrariumRelayLoadingException(f'Could not load hardware for relay {self}: {ex}')

    self._device['device'] = hardware

  @retry(terrariumRelayActionException, tries=3, delay=0.5, max_delay=2, logger=logger)
  def __set_hardware_value(self, state):
    try:
      if self._set_hardware_value(state):
        # Update ok, store the new state
        self._device['value'] = state
      else:
        raise terrariumRelayActionException(f'Error changing relay {self}. Error: unknown')
    except Exception as ex:
      raise terrariumRelayActionException(f'Error changing relay {self}. Error: {ex}')

  @retry(terrariumRelayUpdateException, tries=3, delay=0.5, max_delay=2, logger=logger)
  def __get_hardware_value(self):
    data = None
    try:
      data = self._get_hardware_value()
    except Exception as ex:
      raise terrariumRelayUpdateException(f'Error getting new data from relay {self}. Error: {ex}')

    if data is None:
      raise terrariumRelayUpdateException(f'Error getting new data from relay {self}. Error: unkown')

    return data

  @property
  def id(self):
    if self._device['id'] is None:
      self._device['id'] = md5(f'{self.HARDWARE}{self.address}'.encode()).hexdigest()

    return self._device['id']

  @id.setter
  def id(self, value):
    if value is not None and '' != str(value).strip():
      self._device['id'] = str(value).strip()

  @property
  def address(self):
    return self._device['address']

  @property
  def _address(self):
    return [ part.strip() for part in self.address.split(',') ]

  @address.setter
  def address(self, value):
    value = terrariumUtils.clean_address(value)
    if value is not None and '' != value:
      self._device['address'] = value

  @property
  def device(self):
    return self._device['device']

  @property
  def name(self):
    return self._device['name']

  @name.setter
  def name(self, value):
    if value is not None and '' != str(value).strip():
      self._device['name'] = str(value).strip()

  @property
  def state(self):
    return self._device['value']

  def set_state(self, new_state, force = False):
    if new_state is None or not (self.OFF <= new_state <= self.ON):
      print('Illegal value')
      return False

    changed = False
    # logger.debug('Changing power switch \'{}\' of type \'{}\' at address \'{}\' from state \'{}\' to state \'{}\' (Forced:{})'.format(self.get_name(),
    #                                                                                                                           self.get_type(),
    #                                                                                                                           self.get_address(),
    #                                                                                                                           self.get_state(),
    #                                                                                                                           state,force))

    if self.state != new_state or terrariumUtils.is_true(force):
      old_state = self.state

      try:
        self.__set_hardware_value(new_state)

        #self.state = state
        # logger.info('Changed power switch \'{}\' of type \'{}\' at address \'{}\' from state \'{}\' to state \'{}\' (Forced:{})'.format(self.get_name(),
        #                                                                                                                         self.get_type(),
        #                                                                                                                         self.get_address(),
        #                                                                                                                         old_state,
        #                                                                                                                         state,
        #                                                                                                                         force))


      except Exception as ex:
        print(ex)
        # logger.error('Failed changing power switch \'{}\' of type \'{}\' at address \'{}\' from state \'{}\' to state \'{}\' (Forced:{})'.format(self.get_name(),
        #                                                                                                                                  self.get_type(),
        #                                                                                                                                  self.get_address(),
        #                                                                                                                                  self.get_state(),
        #                                                                                                                                  state,force))

      if (old_state is not None) or (old_state is None and new_state == 0):
        # This is due to a bug that will graph 0 watt usage in the graph after rebooting.
        # Fix is to add power and water usage in constructor
        changed = old_state != self.state

    if changed and self.callback is not None:
      self.callback(self.id, self.state)

    return changed

  def update(self, force = False):
    starttime = time()

    try:
      new_data = self.__get_hardware_value()
    except Exception as ex:
      print('Update relay state error')
      print(ex)
      new_data = None

    self._device['value'] = new_data

    return new_data

#  def on(self, force = False):
  def on(self, value = 100, delay = 0.0):      
    threading.Timer(delay, lambda: self.set_state(value)).start()
    # Not great, but the set_state has a callback for updates
    return True

    #return self.set_state(self.ON, force)

  def off(self, value = 0, delay = 0.0):      
    threading.Timer(delay, lambda: self.set_state(value)).start()
    # Not great, but the set_state has a callback for updates
    return True



  # def off(self, force = False):
  #   return self.set_state(self.OFF, force)

  def is_on(self):
    return self.state == self.ON

  def is_off(self):
    return not self.is_on()

  @property
  def is_dimmer(self):
    return self.HARDWARE.endswith('-dimmer')

  @property
  def type(self):
    return 'dimmer' if self.is_dimmer else 'relay'

  def stop(self):
    pass

  # Auto discovery of running/connected power switches
  @staticmethod
  def scan_relays(callback = None, **kwargs):
    for (hardware_type,relay_device) in terrariumRelay.available_hardware.items():
      #print(f'Scanning for {hardware_type} at {relay_device}')
      try:
        #logger.info(f'Scanning for {relay_device.NAME} relays.')
        for relay in relay_device._scan_relays(callback, **kwargs):
          yield relay
      except AttributeError as ex:
#        print(ex)
        pass

class terrariumRelayDimmer(terrariumRelay):
  TYPE = None

  def __init__(self, id, _, address, name = '', prev_state = None, callback = None):
    super().__init__(id, _, address, name, prev_state, callback)
    self.__dimmer_active = False


  def __running(self, to, duration):
    current_state = self.state
    steps = abs(to - current_state)
    direction = 1 if current_state < to else -1
    pause_time = duration / steps

    print(f'Starting from {current_state} to {to} in {steps} steps. Per step we wait {pause_time} seconds for total {duration} seconds')

    for counter in range(int(steps)):
      current_state += direction
      print(f'Set to state: {current_state}')
      self.set_state(current_state)
      sleep(pause_time)


    self.__dimmer_active = False

  def calibrate(self, data):
    frequency = data.get('dimmer_frequency', self._DIMMER_FREQ)
    if '' == frequency:
      frequency = self._DIMMER_FREQ

    self.device.frequency = int(frequency)

    max_power = int(data.get('dimmer_max_power', -1))
    if 0 <= max_power <= 100:
      self.ON = max_power
      if self.state > self.ON:
        # Current power is higher then the new limit. So lower down the power now!
        self.on(self.ON,0)

  def on(self, value = 100, duration = 0):
#    print(f'Putting {self} on to {value} in {duration} seconds')
    value = max(self.OFF,min(self.ON,value))
#    print(f'Putting {self} on to corrected {value} in {duration} seconds')

    if value == self.state:
      return True

    if 0 == duration:
      self.set_state(value)
      return True

    if not self.__dimmer_active:
      self.__dimmer_active = True
      threading.Thread(target=self.__running,args=(value, duration)).start()
#      print('Thread started....')
      return True

    return False

  def off(self, value = 0, duration = 0):
    return self.on(value,duration)

  def is_on(self):
    return self.state > self.OFF