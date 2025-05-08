#!/usr/bin/env python3

import dbus
import logging
import os
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

LOG_FILE = "/data/GenAutoCurrent/log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# Logging setup
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.info("Starting Generator Derating Monitor with file logging.")

# D-Bus service names and paths
VEBUS_SERVICE_BASE = "com.victronenergy.vebus"
GENERATOR_SERVICE_BASE = "com.victronenergy.generator"
TEMPERATURE_SERVICE_BASE = "com.victronenergy.temperature"
SETTINGS_SERVICE_NAME = "com.victronenergy.settings"
GPS_SERVICE_BASE = "com.victronenergy.gps"
DIGITAL_INPUT_SERVICE_BASE = "com.victronenergy.digitalinput"
SYSTEM_SERVICE = "com.victronenergy.system" # For generator temperature

ALTITUDE_PATH = "/Altitude"
AC_ACTIVE_INPUT_CURRENT_LIMIT_PATH = "/Ac/ActiveIn/CurrentLimit"
TEMPERATURE_PATH = "/Temperature"
CUSTOM_NAME_PATH = "/CustomName"
STATE_PATH = "/State"
PRODUCT_NAME_PATH = "/ProductName"
BUS_ITEM_INTERFACE = "com.victronenergy.BusItem"
GENERATOR_CURRENT_LIMIT_PATH = "/Settings/TransferSwitch/GeneratorCurrentLimit"

# Transfer switch state values
GENERATOR_ON_VALUE = 12
SHORE_POWER_ON_VALUE = 13

# Derating Constants
BASE_TEMPERATURE_THRESHOLD_F = 77.0
TEMP_COEFFICIENT = 0.006
ALTITUDE_COEFFICIENT = 0.00003
BASE_GENERATOR_OUTPUT_AMPS = 62.5
OUTPUT_BUFFER = 0.9
HIGH_GENTEMP_THRESHOLD_F = 222.0
MEDIUM_GENTEMP_THRESHOLD_F = 215.0
HIGH_GENTEMP_REDUCTION = 0.86
MEDIUM_GENTEMP_REDUCTION = 0.93

DEFAULT_ALTITUDE_FEET = 1000.0
DEFAULT_GENERATOR_TEMP_F = 180.0
DEFAULT_OUTDOOR_TEMP_F = 77.0

class GeneratorDeratingMonitor:
    def __init__(self):
        self.bus = dbus.SystemBus()
        self.vebus_service = None
        self.outdoor_temp_service_name = None
        self.generator_temp_service_name = None
        self.gps_service_name = None
        self.transfer_switch_service = None
        self.settings_service_name = SETTINGS_SERVICE_NAME
        self.gen_auto_current_service = None
        self.gen_auto_current_state = None
        self.previous_gen_auto_current_state = None
        self.initial_derated_output_logged = False
        self.initial_altitude = None
        self.initial_outdoor_temp = None
        self.initial_generator_temp = None

        GLib.timeout_add_seconds(2, self._delayed_initialization)

    def _delayed_initialization(self):
        self._find_initial_services()
        self._read_initial_values()
        GLib.timeout_add(5000, self._periodic_monitoring)
        return GLib.SOURCE_REMOVE

    def _find_initial_services(self):
        self.vebus_service = self._find_service(VEBUS_SERVICE_BASE)
        self._find_outdoor_temperature_service()
        self._find_generator_temperature_service()
        self._find_gps_service()
        self._find_transfer_switch_input()
        self._find_gen_auto_current_input()

        logging.info(f"VE.Bus service: {self.vebus_service}")
        logging.info(f"Outdoor temp service: {self.outdoor_temp_service_name}")
        logging.info(f"Generator temp service: {self.generator_temp_service_name}")
        logging.info(f"GPS service: {self.gps_service_name}")
        logging.info(f"Transfer switch service: {self.transfer_switch_service}")
        logging.info(f"'Gen Auto Current' service: {self.gen_auto_current_service}")

    def _read_initial_values(self):
        self._update_outdoor_temperature(log_update=False, log_initial=True)
        self._update_altitude(log_update=False, log_initial=True)
        self._update_generator_temperature(log_update=False, log_initial=True)
        self._update_gen_auto_current_state()
        self.previous_gen_auto_current_state = self.gen_auto_current_state

    def _find_service(self, service_base):
        services = [name for name in self.bus.list_names() if name.startswith(service_base)]
        return services[0] if services else None

    def _get_dbus_value(self, service_name, path):
        try:
            obj = self.bus.get_object(service_name, path)
            interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
            return interface.GetValue()
        except Exception as e:
            logging.error(f"Error getting value from {service_name}{path}: {e}")
            return None

    def _set_dbus_value(self, service_name, path, value):
        try:
            obj = self.bus.get_object(service_name, path)
            interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
            interface.SetValue(dbus.Double(value))
            logging.info(f"Set {service_name}{path} to {value}")
        except Exception as e:
            logging.error(f"Error setting value for {service_name}{path} to {value}: {e}")

    def _find_outdoor_temperature_service(self, retries=3, delay=1):
        temperature_services = [name for name in self.bus.list_names() if name.startswith(TEMPERATURE_SERVICE_BASE)]
        logging.info(f"Found temperature services (attempt {retries}): {temperature_services}")
        for service_name in temperature_services:
            try:
                obj = self.bus.get_object(service_name, CUSTOM_NAME_PATH)
                interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
                custom_name = interface.GetValue()
                logging.info(f"Checking service: {service_name}, CustomName: '{custom_name}'")
                if custom_name and "Outdoor" in custom_name:
                    logging.info(f"Found potential outdoor temperature sensor: {service_name} (CustomName: {custom_name})")
                    self.outdoor_temp_service_name = service_name
                    return
            except Exception as e:
                logging.debug(f"Error checking CustomName for {service_name}: {e}")
        if retries > 0 and not self.outdoor_temp_service_name:
            logging.warning(f"Could not find outdoor temperature sensor. Retrying in {delay} second(s)...")
            import time
            time.sleep(delay)
            self._find_outdoor_temperature_service(retries - 1, delay)
        elif not self.outdoor_temp_service_name:
            logging.warning("Could not automatically find an outdoor temperature sensor with 'Outdoor' in CustomName after multiple retries.")

    def _find_generator_temperature_service(self):
        temperature_services = [name for name in self.bus.list_names() if name.startswith(TEMPERATURE_SERVICE_BASE)]
        for service_name in temperature_services:
            try:
                obj = self.bus.get_object(service_name, CUSTOM_NAME_PATH)
                interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
                custom_name = interface.GetValue()
                if custom_name and any(keyword in custom_name for keyword in ["gen", "Gen", "generator", "Generator"]):
                    logging.info(f"Found potential generator temperature sensor: {service_name} (CustomName: {custom_name})")
                    self.generator_temp_service_name = service_name
                    return
            except Exception as e:
                logging.debug(f"Error checking CustomName for {service_name}: {e}")

            try:
                obj = self.bus.get_object(service_name, PRODUCT_NAME_PATH)
                interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
                product_name = interface.GetValue()
                if product_name and any(keyword in product_name for keyword in ["gen", "Gen", "generator", "Generator"]):
                    logging.info(f"Found potential generator temperature sensor: {service_name} (ProductName: {product_name})")
                    self.generator_temp_service_name = service_name
                    return
            except Exception as e:
                logging.debug(f"Error checking ProductName for {service_name}: {e}")

    def _find_gps_service(self):
        self.gps_service_name = self._find_service(GPS_SERVICE_BASE)
        logging.info(f"GPS service found: {self.gps_service_name}")

    def _find_transfer_switch_input(self):
        service_names = [name for name in self.bus.list_names() if name.startswith(DIGITAL_INPUT_SERVICE_BASE)]
        for service_name in service_names:
            try:
                obj = self.bus.get_object(service_name, PRODUCT_NAME_PATH)
                interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
                product_name = interface.GetValue()
                if product_name and ("Transfer Switch" in product_name or "transfer switch" in product_name):
                    logging.info(f"Found External AC Transfer Switch input: {service_name} (ProductName: '{product_name}')")
                    self.transfer_switch_service = service_name
                    return
            except Exception as e:
                logging.debug(f"Error checking product name for {service_name}: {e}")
        logging.warning("Could not find a digital input with 'Transfer Switch' in its product name.")

    def _find_gen_auto_current_input(self):
        service_names = [name for name in self.bus.list_names() if name.startswith(DIGITAL_INPUT_SERVICE_BASE)]
        for service_name in service_names:
            try:
                obj = self.bus.get_object(service_name, PRODUCT_NAME_PATH)
                interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
                product_name = interface.GetValue()
                if product_name and ("Gen Auto Current" in product_name or "gen auto current" in product_name):
                    logging.info(f"Found 'Gen Auto Current' input: {service_name} (ProductName: '{product_name}')")
                    self.gen_auto_current_service = service_name
                    return
            except Exception as e:
                logging.debug(f"Error checking product name for {service_name}: {e}")
        logging.warning("Could not find a digital input with 'Gen Auto Current' in its product name.")

    def _update_outdoor_temperature(self, log_update=True, log_initial=False):
        if self.outdoor_temp_service_name:
            temp_celsius = self._get_dbus_value(self.outdoor_temp_service_name, TEMPERATURE_PATH)
            if temp_celsius is not None:
                self.outdoor_temp_fahrenheit = (temp_celsius * 9/5) + 32
                if log_initial and self.initial_outdoor_temp is None:
                    self.initial_outdoor_temp = self.outdoor_temp_fahrenheit
                    logging.info(f"Initial Outdoor Temperature: {self.initial_outdoor_temp:.2f} F")
                elif log_update:
                    logging.debug(f"Updated outdoor temperature: {self.outdoor_temp_fahrenheit:.2f} F")

    def _update_altitude(self, log_update=True, log_initial=False):
        if self.gps_service_name:
            altitude_meters = self._get_dbus_value(self.gps_service_name, ALTITUDE_PATH)
            if altitude_meters is not None:
                self.altitude_feet = altitude_meters * 3.28084
                if log_initial and self.initial_altitude is None:
                    self.initial_altitude = self.altitude_feet
                    logging.info(f"Initial Altitude: {self.initial_altitude:.2f} feet")
                elif log_update:
                    logging.debug(f"Updated altitude: {self.altitude_feet:.2f} feet")

    def _update_generator_temperature(self, log_update=True, log_initial=False):
        if self.generator_temp_service_name:
            temp_celsius = self._get_dbus_value(self.generator_temp_service_name, TEMPERATURE_PATH)
            if temp_celsius is not None:
                self.generator_temp_fahrenheit = (temp_celsius * 9/5) + 32
                if log_initial and self.initial_generator_temp is None:
                    self.initial_generator_temp = self.generator_temp_fahrenheit
                    logging.info(f"Initial Generator Temperature: {self.initial_generator_temp:.2f} F")
                elif log_update and self.generator_temp_fahrenheit > 212.0:
                    logging.debug(f"Generator temperature above threshold: {self.generator_temp_fahrenheit:.2f} F")
                elif log_update:
                    logging.debug(f"Generator temperature: {self.generator_temp_fahrenheit:.2f} F (below threshold)")
            else:
                logging.debug("Could not retrieve generator temperature from D-Bus.")

    def _update_gen_auto_current_state(self):
        if self.gen_auto_current_service:
            state = self._get_dbus_value(self.gen_auto_current_service, STATE_PATH)
            if state is not None:
                if state != self.previous_gen_auto_current_state:
                    self.gen_auto_current_state = state
                    logging.info(f"'Gen Auto Current' state changed to: {self.gen_auto_current_state}")
                else:
                    self.gen_auto_current_state = state
            else:
                logging.debug("Could not retrieve 'Gen Auto Current' state from D-Bus.")

    def _is_generator_running(self):
        if self.transfer_switch_service:
            state = self._get_dbus_value(self.transfer_switch_service, STATE_PATH)
            return state == GENERATOR_ON_VALUE
        return False

    def calculate_derating_factor(self, temperature_fahrenheit, altitude_feet, generator_temperature_fahrenheit):
        temperature_multiplier = 1.0
        altitude_multiplier = 1.0
        generator_temp_multiplier = 1.0

        if temperature_fahrenheit is not None:
            if temperature_fahrenheit > BASE_TEMPERATURE_THRESHOLD_F:
                temperature_multiplier = 1.0 - ((temperature_fahrenheit - BASE_TEMPERATURE_THRESHOLD_F) * TEMP_COEFFICIENT)
                temperature_multiplier = max(0.0, temperature_multiplier)

        if altitude_feet is not None:
            altitude_multiplier = 1.0 - (altitude_feet * ALTITUDE_COEFFICIENT)
            altitude_multiplier = max(0.0, altitude_multiplier)

        if generator_temperature_fahrenheit is not None:
            if generator_temperature_fahrenheit >= HIGH_GENTEMP_THRESHOLD_F:
                generator_temp_multiplier = HIGH_GENTEMP_REDUCTION
            elif generator_temperature_fahrenheit >= MEDIUM_GENTEMP_THRESHOLD_F:
                generator_temp_multiplier = MEDIUM_GENTEMP_REDUCTION

        return temperature_multiplier * altitude_multiplier * generator_temp_multiplier * OUTPUT_BUFFER

    def _perform_derating(self):
        if self.outdoor_temp_fahrenheit is not None and self.altitude_feet is not None and self.generator_temp_fahrenheit is not None:
            derating_factor = self.calculate_derating_factor(
                self.outdoor_temp_fahrenheit, self.altitude_feet, self.generator_temp_fahrenheit
            )
            rounded_output = round(derated_output_amps, 1)

            # Set the AC Active Input Current Limit on VE.Bus when generator is running
            if self.vebus_service and self._is_generator_running():
                self._set_dbus_value(self.vebus_service, AC_ACTIVE_INPUT_CURRENT_LIMIT_PATH, rounded_output)
                logging.debug(f"Generator running,set VE.Bus AC Active Input Current Limit: {rounded_output:.1f} Amps")
            elif self.vebus_service:
                logging.debug("Generator not running, VE.Bus AC Active Input Current Limit not actively adjusted by derating.")

            # Store the derated current limit in the settings path for the transfer switch
            if not self.initial_derated_output_logged:
                self._set_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH, rounded_output)
                logging.info(f"Initial Transfer Switch Generator Current Limit: {rounded_output:.1f} Amps")
                logging.info(f"Initial Calculated Derated Generator Output: {rounded_output:.1f} Amps")
                self.initial_derated_output_logged = True
            else:
                logging.debug(f"Calculated Derated Generator Output: {rounded_output:.1f} Amps (value might have changed)")

        else:
            logging.debug("Not all temperature or altitude data available for derating.")

    def _periodic_monitoring(self):
        self._update_outdoor_temperature()
        self._update_altitude()
        self._update_generator_temperature()
        self._update_gen_auto_current_state()

        # Perform derating only if Gen Auto Current is on (state 3)
        if self.gen_auto_current_state == 3:
            self._perform_derating()

        return True

def main():
    DBusGMainLoop(set_as_default=True)
    GeneratorDeratingMonitor()
    mainloop = GLib.MainLoop()
    mainloop.run()

if __name__ == "__main__":
    main()
