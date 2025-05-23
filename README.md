########## Purpose of this service ################

This service is intended to monitor a digital input that is connected to the automatic transfer switch of a remote generator.
When the digital input changes state, indicating that the remote generator is running, and the transfer switch has 'transfered' to the generator,
this service will change the ac input source (of the connected victron multiplus or quatro) to 'generator' and change the active current limit to the previously
stored value. When the digital input changes state again, (indicating that the generator is no longer running), then the ac input source will
be changed back to 'grid' or 'shore' and the saved grid/shore current limit restored.

########## First time setup ###############

1. Set one of the digital input types to 'bilge pump' (not bilge alarm) and rename it to 'Transfer Switch' so that this service can identify it.
2. Connect this digital input to a dry contact relay which is to be triggered by the automatic transfer switch when the generator is running.
3. When the generator is running the 'Transfer Switch' digital input state should show 'On', and 'Off' when it is not. If the reverse is true, then select 'invert' in the device settings for the digital input.
4. Install this package with kevin windrem's setup helper. packagename: TransferSwitch github user: drtinaz tag/branch: main
5. With the generator off, set the active ac input source to either grid or shore, whichever one you desire.
6. Set the ac current limit desired.
7. Start the generator and verify that the 'Transfer Switch' digital input is now showing 'On'.
8. Set the ac input source to generator if it is not already.
9. Set the desired generator current limit.
10. Shut off the generator and verify that the active ac input source and current limit are now being restored to the previous values.
