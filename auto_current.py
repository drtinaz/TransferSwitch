#!/usr/bin/env python3

import dbus
import logging
import os
import sys
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
sys.path.insert(1, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
from ve_utils import wrap_dbus_value

LOG_FILE = "/data/GenAutoCurrent/log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# Logging setup (change level of logging level=logging.INFO or level=logging.DEBUG)
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

# Transfer switch state values (both original and new values are listed)
GENERATOR_ON_VALUE = (12, 3) # Original value 12, new value 3
SHORE_POWER_ON_VALUE = (13, 2) # Original value 13, new value 2

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
        self.previous_gen_auto_current_state = None # Initialize here
        self.initial_derated_output_logged = False
        self.initial_altitude = None
        self.initial_outdoor_temp = None
        self.initial_generator_temp = None
        self.previous_ac_current_limit = None # New variable to store the previously set AC current limit
        self.previous_generator_current_limit_setting = None # Track changes in the generator current limit setting

        # START CORRECTION
        self.outdoor_temp_fahrenheit = DEFAULT_OUTDOOR_TEMP_F
        self.altitude_feet = DEFAULT_ALTITUDE_FEET
        self.generator_temp_fahrenheit = DEFAULT_GENERATOR_TEMP_F
        # END CORRECTION
        
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
        self._update_gen_auto_current_state(initial_read=True)
        # Initial read of the generator current limit setting
        current_limit = self._get_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH)
        if current_limit is not None:
            self.previous_generator_current_limit_setting = round(float(current_limit), 1)
            logging.info(f"Initial Generator Current Limit setting: {self.previous_generator_current_limit_setting:.1f} Amps")

        # Initial read of the AC active input current limit for the new feature
        ac_limit = self._get_dbus_value(self.vebus_service, AC_ACTIVE_INPUT_CURRENT_LIMIT_PATH)
        if ac_limit is not None:
            self.previous_ac_current_limit = round(float(ac_limit), 1)
            logging.info(f"Initial VE.Bus AC Active Input Current Limit: {self.previous_ac_current_limit:.1f} Amps")


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
            # Use wrap_dbus_value here
            interface.SetValue(wrap_dbus_value(value))
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

    def _update_gen_auto_current_state(self, initial_read=False):
        if self.gen_auto_current_service:
            state = self._get_dbus_value(self.gen_auto_current_service, STATE_PATH)
            if state is not None:
                if initial_read:
                    self.gen_auto_current_state = state
                    self.previous_gen_auto_current_state = state
                    logging.info(f"Initial 'Gen Auto Current' state: {self.gen_auto_current_state}")
                elif state != self.previous_gen_auto_current_state:
                    self.previous_gen_auto_current_state = self.gen_auto_current_state
                    self.gen_auto_current_state = state
                    logging.info(f"'Gen Auto Current' state changed to: {self.gen_auto_current_state}")
                else:
                    self.gen_auto_current_state = state # Keep current state updated even if not logged
                    logging.debug(f"'Gen Auto Current' state remains: {self.gen_auto_current_state}")
            else:
                logging.debug("Could not retrieve 'Gen Auto Current' state from D-Bus.")

    def _is_generator_running(self):
        if self.transfer_switch_service:
            state = self._get_dbus_value(self.transfer_switch_service, STATE_PATH)
            # Check if the state is in the GENERATOR_ON_VALUE tuple
            return state in GENERATOR_ON_VALUE
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
            # START CORRECTION
            derated_output_amps = BASE_GENERATOR_OUTPUT_AMPS * derating_factor
            rounded_output = round(derated_output_amps, 1)
            # END CORRECTION

            # Store the derated current limit in the settings path for the transfer switch
            # START CORRECTION - Refined logic and logging for settings update
            current_generator_limit_setting = self._get_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH)

            if not self.initial_derated_output_logged:
                self._set_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH, rounded_output)
                logging.info(f"Initial Transfer Switch Generator Current Limit set to: {rounded_output:.1f} Amps (due to auto derating)")
                self.initial_derated_output_logged = True
            elif current_generator_limit_setting is None or abs(current_generator_limit_setting - rounded_output) > 0.01: # Only log if the value actually changes significantly
                self._set_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH, rounded_output)
                logging.info(f"Transfer Switch Generator Current Limit updated to: {rounded_output:.1f} Amps (due to auto derating)")
            else:
                logging.debug(f"Transfer Switch Generator Current Limit remains: {rounded_output:.1f} Amps")
            # END CORRECTION

        else:
            # START CORRECTION - Log level change
            logging.warning("Not all temperature or altitude data available for derating. Skipping calculation.")
            # END CORRECTION

    def _sync_generator_limit_to_ac_input(self):
        if self.vebus_service and self._is_generator_running():
            current_generator_limit_setting = self._get_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH)
            if current_generator_limit_setting is not None:
                rounded_gen_limit = round(float(current_generator_limit_setting), 1)

                # Check if the generator limit has changed or if the AC input limit needs to be set initially
                if self.previous_generator_current_limit_setting is None or abs(self.previous_generator_current_limit_setting - rounded_gen_limit) > 0.01:
                    self._set_dbus_value(self.vebus_service, AC_ACTIVE_INPUT_CURRENT_LIMIT_PATH, rounded_gen_limit)
                    logging.info(f"Generator running: Synced VE.Bus AC Active Input Current Limit to Generator Current Limit ({rounded_gen_limit:.1f} Amps).")
                    self.previous_ac_current_limit = rounded_gen_limit # Keep previous_ac_current_limit in sync
                    self.previous_generator_current_limit_setting = rounded_gen_limit # Update the previous generator limit setting
                else:
                    logging.debug(f"Generator running: VE.Bus AC Active Input Current Limit already matches Generator Current Limit ({rounded_gen_limit:.1f} Amps).")
            else:
                logging.warning("Could not retrieve Generator Current Limit setting. Cannot sync to AC input.")
        elif self.vebus_service:
            logging.debug("Generator not running, AC Active Input Current Limit not synced from generator current limit setting.")


    def _sync_generator_limit_from_ac_input(self):
        """
        Synchronizes the generator current limit to the AC input limit
        when the generator is running and 'Gen Auto Current' is off/disabled.
        This helps prevent a looping problem by only reacting to external changes
        in the AC input limit.
        """
        # I'm assuming that 'Gen Auto Current' being 'on/enabled' corresponds to the NEW_GENERATOR_ON_VALUE (3)
        # and that any other state means it's 'off/disabled'.
        if self.vebus_service and self._is_generator_running() and self.gen_auto_current_state != 3:
            current_ac_limit = self._get_dbus_value(self.vebus_service, AC_ACTIVE_INPUT_CURRENT_LIMIT_PATH)
            if current_ac_limit is not None:
                rounded_ac_limit = round(float(current_ac_limit), 1)

                if self.previous_ac_current_limit is None or abs(rounded_ac_limit - self.previous_ac_current_limit) > 0.01:
                    # Only set the generator current limit if it's different
                    current_gen_limit = self._get_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH)
                    if current_gen_limit is None or abs(current_gen_limit - rounded_ac_limit) > 0.01:
                        self._set_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH, rounded_ac_limit)
                        logging.info(f"Generator running and 'Gen Auto Current' is OFF/DISABLED: Synced Generator Current Limit to VE.Bus AC Active Input Current Limit ({rounded_ac_limit:.1f} Amps).")
                        self.previous_generator_current_limit_setting = rounded_ac_limit # Keep previous gen limit in sync

                    self.previous_ac_current_limit = rounded_ac_limit # Update the previous AC limit to prevent looping
                else:
                    logging.debug(f"Generator running and 'Gen Auto Current' is OFF/DISABLED: VE.Bus AC Active Input Current Limit ({rounded_ac_limit:.1f} Amps) has not changed.")
            else:
                logging.warning("Could not retrieve VE.Bus AC Active Input Current Limit. Cannot sync to generator current limit.")
        elif self.vebus_service:
            if not self._is_generator_running():
                logging.debug("Generator not running, AC Active Input Current Limit not synced to generator current limit.")
            elif self.gen_auto_current_state == 3:
                logging.debug("'Gen Auto Current' is ON (3), AC Active Input Current Limit not synced to generator current limit.")

    def _periodic_monitoring(self):
        #logging.info(">>>> _periodic_monitoring heartbeat <<<<") # Re-inserted (original line)
        self._update_outdoor_temperature()
        self._update_altitude()
        self._update_generator_temperature()
        self._update_gen_auto_current_state()

        # Always attempt to sync the generator current limit to the AC input limit if the generator is running.
        # This is the *original* sync behavior where the generator's limit sets the AC input limit.
        self._sync_generator_limit_to_ac_input()

        # New logic: Sync generator current limit FROM AC input limit when generator is running and Gen Auto Current is OFF/DISABLED.
        self._sync_generator_limit_from_ac_input()

        # Perform derating only if Gen Auto Current is on (state 3)
        if self.gen_auto_current_state == 3:
            self._perform_derating()
        # START CORRECTION
        else:
            logging.debug(f"Gen Auto Current state is not 3. Current state: {self.gen_auto_current_state}")
        # END CORRECTION

        return True

def main():
    DBusGMainLoop(set_as_default=True)
    GeneratorDeratingMonitor()
    mainloop = GLib.MainLoop()
    mainloop.run()

if __name__ == "__main__":
    main()
