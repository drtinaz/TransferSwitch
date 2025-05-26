#!/usr/bin/env python

# This program integrates an external transfer switch ahead of the single AC input
# of a MultiPlus or Quattro inverter/charger.
#
# When the external transfer switch changes between grid and generator the data for that input must be switched between
#  grid and generator settings
#
# These two sets of settings are stored in dbus Settings.
# When the transfer switch digital input changes, this program switches
#    the Multiplus settings between these two stored values
# When the user changes the settings, the grid or generator-specific Settings are updated
#
# In order to function, one of the digital inputs must be set to Bilge Pump (NOT bilge alarm) and the custom name changed to 'Transfer Switch'
# This input should be connected to a contact closure on the external transfer switch to indicate
#    which of it's sources is switched to its output
#
# For Quattro, the /Settings/TransferSwitch/TransferSwitchOnAc2 tells this program where the transfer switch is connected:
#    0 if connected to AC 1 In
#    1 if connected to AC 2 In
# credit given to Kevin Windrem for the original package, from which this package is based upon.

import platform
import argparse
import logging
import sys
import subprocess
import os
import time
import dbus
from gi.repository import GLib
sys.path.insert(1, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
from vedbus import VeDbusService
from ve_utils import wrap_dbus_value
from settingsdevice import SettingsDevice


dbusSettingsPath = "com.victronenergy.settings"
dbusSystemPath = "com.victronenergy.system"


class Monitor:

    def getVeBusObjects (self):
        vebusService = ""

        # invalidate all local parameters if transfer switch is not active
        if not self.transferSwitchActive:
            # release generator override if it's still active
            try:
                if self.remoteGeneratorSelectedItem != None:
                    self.remoteGeneratorSelectedItem.SetValue (wrap_dbus_value (0))
            except:
                logging.error ("could not release /Ac/Control/RemoteGeneratorSelected")
                pass
            self.remoteGeneratorSelectedItem = None
            self.remoteGeneratorSelectedLocalValue = -1
            self.dbusOk = False
            self.numberOfAcInputs = 0
            self.acInputTypeObj = None
            self.veBusService = ""
            self.transferSwitchLocation = 0
            return

        try:
            obj = self.theBus.get_object (dbusSystemPath, '/VebusService')
            vebusService = obj.GetText ()
        except:
            if self.dbusOk:
                logging.info ("Multi/Quattro disappeared - /VebusService invalid")
            self.veBusService = ""
            self.dbusOk = False
            self.numberOfAcInputs = 0
            self.acInputTypeObj = None

        if vebusService == "---":
            if self.veBusService != "":
                logging.info ("Multi/Quattro disappeared")
            self.veBusService = ""
            self.dbusOk = False
            self.numberOfAcInputs = 0
        elif self.veBusService == "" or vebusService != self.veBusService:
            self.veBusService = vebusService
            try:
                self.numberOfAcInputs = self.theBus.get_object (vebusService, "/Ac/NumberOfAcInputs").GetValue ()
            except:
                self.numberOfAcInputs = 0
            try:
                self.remoteGeneratorSelectedItem = self.theBus.get_object (vebusService,
                    "/Ac/Control/RemoteGeneratorSelected")
            except:
                self.remoteGeneratorSelectedItem = None
                self.remoteGeneratorSelectedLocalValue = -1

            if self.numberOfAcInputs == 0:
                self.dbusOk = False
            elif self.numberOfAcInputs == 2:
                logging.info ("discovered Quattro at " + vebusService)
            elif self.numberOfAcInputs == 1:
                logging.info ("discovered Multi at " + vebusService)            

            try:
                self.currentLimitObj = self.theBus.get_object (vebusService, "/Ac/ActiveIn/CurrentLimit")
                self.currentLimitIsAdjustableObj = self.theBus.get_object (vebusService, "/Ac/ActiveIn/CurrentLimitIsAdjustable")
            except:
                logging.error ("current limit dbus setup failed - changes can't be made")
                self.dbusOk = False

        # check to see where the transfer switch is connected
        if self.numberOfAcInputs == 0:
            transferSwitchLocation = 0
        elif self.numberOfAcInputs == 1:
            transferSwitchLocation = 1
        elif self.DbusSettings['transferSwitchOnAc2'] == 1:
            transferSwitchLocation = 2
        else:
            transferSwitchLocation = 1        

        # if changed, trigger refresh of object pointers
        if transferSwitchLocation != self.transferSwitchLocation:
            if transferSwitchLocation != 0:
                logging.info ("Transfer switch is on AC %d in" % transferSwitchLocation)
            self.transferSwitchLocation = transferSwitchLocation
            try:
                if self.transferSwitchLocation == 2:
                    self.acInputTypeObj = self.theBus.get_object (dbusSettingsPath, "/Settings/SystemSetup/AcInput2")
                else:
                    self.acInputTypeObj = self.theBus.get_object (dbusSettingsPath, "/Settings/SystemSetup/AcInput1")
                self.dbusOk = True
            except:
                self.dbusOk = False
                logging.error ("AC input dbus setup failed - changes can't be made")


    def updateTransferSwitchState (self):
        inputValid = False
        # If a transfer switch input is currently active, check its name
        if self.transferSwitchActive and self.transferSwitchNameObj:
            try:
                name = self.transferSwitchNameObj.GetValue()
                if self.extTransferDigInputName.lower() in name.lower():
                    # Name matches, now check the state
                    state = self.transferSwitchStateObj.GetValue()
                    # Updated state check: 12 or 3 for onGenerator (true), 13 or 2 for not onGenerator (false)
                    if state in (12, 3):  # On generator
                        inputValid = True
                        self.onGenerator = True
                    elif state in (13, 2): # On grid
                        inputValid = True
                        self.onGenerator = False
                else:
                    logging.info("Current transfer switch input name '%s' does not match '%s'", name, self.extTransferDigInputName)
            except dbus.exceptions.DBusException as e:
                logging.error("Error accessing transfer switch D-Bus object: %s", e)
                # If there's a D-Bus error, assume the input is no longer valid
                inputValid = False
            except Exception as e:
                logging.error("An unexpected error occurred: %s", e)
                inputValid = False


        if not inputValid and self.transferSwitchActive:
            logging.info ("Transfer switch digital input no longer valid or name mismatch")
            self.transferSwitchActive = False
            self.transferSwitchNameObj = None # Clear the name object as it's no longer valid

        # current digital input (if any) not valid or name mismatch
        # search for a new one only every 10 seconds to avoid unnecessary processing
        elif not inputValid and self.tsInputSearchDelay >= 10:
            newInputService = ""
            for service in self.theBus.list_names():
                # found a digital input service, now check for custom name and valid state
                if service.startswith ("com.victronenergy.digitalinput"):
                    try:
                        name_obj = self.theBus.get_object(service, '/CustomName')
                        custom_name = name_obj.GetValue()
                        if self.extTransferDigInputName.lower() in custom_name.lower():
                            state_obj = self.theBus.get_object (service, '/State')
                            state = state_obj.GetValue()
                            # found it! Check for new state values
                            if state in (12, 3) or state in (13, 2):
                                newInputService = service
                                self.transferSwitchNameObj = name_obj # Store the name object
                                self.transferSwitchStateObj = state_obj # Store the state object
                                break
                    # ignore errors - continue to check for other services
                    except dbus.exceptions.DBusException as e:
                        # This typically means /CustomName or /State doesn't exist for this service
                        # logging.debug("D-Bus error for service %s: %s", service, e) # Too verbose for regular logging
                        pass
                    except Exception as e:
                        logging.error("An unexpected error occurred while searching for digital inputs: %s", e)


            # found new service - set up to use its values
            if newInputService != "":
                logging.info ("discovered transfer switch digital input service at %s with custom name '%s'", newInputService, custom_name)
                self.transferSwitchActive = True
            elif self.transferSwitchActive: # This case should ideally not be hit if newInputService is "" but transferSwitchActive is True
                logging.info ("Transfer switch digital input service NOT found with matching name")
                self.transferSwitchActive = False

        if self.transferSwitchActive:
            self.tsInputSearchDelay = 0
        else:
            self.onGenerator = False
            # if search delay timer is active, increment it now
            if self.tsInputSearchDelay < 10:
                self.tsInputSearchDelay += 1
            else:
                self.tsInputSearchDelay = 0


    def transferToGrid (self):
        if self.dbusOk:
            logging.info ("switching to grid settings")
            # save current values for restore when switching back to generator
            try:
                self.DbusSettings['generatorCurrentLimit'] = self.currentLimitObj.GetValue ()
            except:
                logging.error ("dbus error generator AC input current limit not saved switching to grid")

            try:
                self.acInputTypeObj.SetValue (self.DbusSettings['gridInputType'])
            except:
                logging.error ("dbus error AC input type not changed to grid")
            try:
                if self.currentLimitIsAdjustableObj.GetValue () == 1:
                    self.currentLimitObj.SetValue (wrap_dbus_value (self.DbusSettings['gridCurrentLimit']))
                else:
                    logging.warning ("Input current limit not adjustable - not changed")
            except:
                logging.error ("dbus error AC input current limit not changed switching to grid")

    def transferToGenerator (self):
        if self.dbusOk:
            logging.info ("switching to generator settings")
            # save current values for restore when switching back to grid
            try:
                inputType = self.acInputTypeObj.GetValue ()
                # grid input type can only be either 1 (grid) or 3 (shore)
                #    patch this up to prevent issues later
                if inputType == 2:
                    logging.warning ("grid input can not be generator - setting to grid")
                    inputType = 1
                self.DbusSettings['gridInputType'] = inputType
            except:
                logging.error ("dbus error AC input type not saved when switching to generator")
            try:
                self.DbusSettings['gridCurrentLimit'] = self.currentLimitObj.GetValue ()
            except:
                logging.error ("dbus error AC input current limit not saved when switching to generator")

            try:
                self.acInputTypeObj.SetValue (2)
            except:
                logging.error ("dbus error AC input type not changed when switching to generator")
            try:
                if self.currentLimitIsAdjustableObj.GetValue () == 1:
                    self.currentLimitObj.SetValue (wrap_dbus_value (self.DbusSettings['generatorCurrentLimit']))
                else:
                    logging.warning ("Input current limit not adjustable - not changed")
            except:
                logging.error ("dbus error AC input current limit not changed when switching to generator")


    def background (self):

        ##startTime = time.time()
        self.updateTransferSwitchState ()
        self.getVeBusObjects ()

        # skip processing if any dbus paramters were not initialized properly
        if self.dbusOk and self.transferSwitchActive:
            # process transfer switch state change
            if self.lastOnGenerator != None and self.onGenerator != self.lastOnGenerator:
                if self.onGenerator:
                    self.transferToGenerator ()
                else:
                    self.transferToGrid ()
            self.lastOnGenerator = self.onGenerator
        elif self.onGenerator:
            self.transferToGrid ()

        # update main VE.Bus RemoteGeneratorSelected which is used to enable grid charging
        #    if renewable energy is turned on
        if not self.dbusOk or not self.onGenerator:
            newRemoteGeneratorSelectedLocalValue = 0
        else:
            newRemoteGeneratorSelectedLocalValue = 1
        if self.remoteGeneratorSelectedItem == None:
            self.remoteGeneratorSelectedLocalValue = -1
        elif newRemoteGeneratorSelectedLocalValue != self.remoteGeneratorSelectedLocalValue:
            try:
                self.remoteGeneratorSelectedItem.SetValue (wrap_dbus_value (newRemoteGeneratorSelectedLocalValue))
            except:
                logging.error ("could not set /Ac/Control/RemoteGeneratorSelected")
                pass

            self.remoteGeneratorSelectedLocalValue = newRemoteGeneratorSelectedLocalValue

        ##stopTime = time.time()
        ##print ("#### background time %0.3f" % (stopTime - startTime))
        return True


    def __init__(self):
        self.theBus = dbus.SystemBus()
        self.onGenerator = False
        self.veBusService = ""
        self.lastVeBusService = ""
        self.acInputTypeObj = None
        self.numberOfAcInputs = 0
        self.currentLimitObj = None
        self.currentLimitIsAdjustableObj = None
        self.remoteGeneratorSelectedItem = None
        self.remoteGeneratorSelectedLocalValue = -1

        self.transferSwitchStateObj = None
        self.transferSwitchNameObj = None # New attribute to store the D-Bus object for CustomName
        self.extTransferDigInputName = "transfer switch"    # Changed to just 'transfer switch' as the key phrase

        self.lastOnGenerator = None
        self.transferSwitchActive = False
        self.dbusOk = False
        self.transferSwitchLocation = 0
        self.tsInputSearchDelay = 99 # allow search to occur immediately

        # create / attach local settings
        settingsList = {
            'gridCurrentLimit': [ '/Settings/TransferSwitch/GridCurrentLimit', 0.0, 0.0, 0.0 ],
            'generatorCurrentLimit': [ '/Settings/TransferSwitch/GeneratorCurrentLimit', 0.0, 0.0, 0.0 ],
            'gridInputType': [ '/Settings/TransferSwitch/GridType', 0, 0, 0 ],
            'stopWhenAcAvaiable': [ '/Settings/TransferSwitch/StopWhenAcAvailable', 0, 0, 0 ],
            'stopWhenAcAvaiableFp': [ '/Settings/TransferSwitch/StopWhenAcAvailableFp', 0, 0, 0 ],
            'transferSwitchOnAc2': [ '/Settings/TransferSwitch/TransferSwitchOnAc2', 0, 0, 0 ],
                        }
        self.DbusSettings = SettingsDevice(bus=self.theBus, supportedSettings=settingsList,
                                timeout = 10, eventCallback=None )

        # grid input type should be either 1 (grid) or 3 (shore)
        #    patch this up to prevent issues later
        if self.DbusSettings['gridInputType'] == 2:
            logging.warning ("grid input type was generator - resetting to grid")
            self.DbusSettings['gridInputType'] = 1

        GLib.timeout_add (1000, self.background)
        return None

def main():

    from dbus.mainloop.glib import DBusGMainLoop

    # Define the log file path and directory
    log_file_path = '/data/TransferSwitch/log'
    log_dir = os.path.dirname(log_file_path)

    # Ensure the log directory exists
    if not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
        except OSError as e:
            # Fallback to console logging if directory creation fails
            logging.basicConfig(level=logging.INFO)
            logging.error(f"Failed to create log directory {log_dir}: {e}. Logging to console.")
            # Continue to main logic, but logging will be to console
            pass # continue to main logic, but logging will be to console

    # Configure logging to a file, or if dir creation failed, it's already set to console
    if not logging.getLogger().handlers: # Only configure if no handlers are already set (e.g. from fallback)
        try:
            logging.basicConfig(level=logging.INFO, filename=log_file_path, filemode='a',
                                format='%(asctime)s - %(levelname)s - %(message)s')
        except Exception as e:
            # Fallback to console logging if file logging setup fails
            logging.basicConfig(level=logging.INFO)
            logging.error(f"Failed to set up file logging to {log_file_path}: {e}. Logging to console.")


    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)

    # Logging start message without version
    logging.info (">>>>>>>>>>>>>>>> Transfer Switch Monitor starting <<<<<<<<<<<<<<<<")

    Monitor ()

    mainloop = GLib.MainLoop()
    mainloop.run()

main()
