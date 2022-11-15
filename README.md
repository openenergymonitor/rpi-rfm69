# OpenEnergyMonitor fork of jgillula/rpi-rfm69 (v0.3.0)

Note from Trystan:

This is a fork of v0.3.0 of the jgillula/rpi-rfm69 library. v0.3.0 choosen due to simpler packet queue design without the more recent work on making the library thread safe. Im not 100% sure if this is the best choice but have found it easier to work with so far within the context of the EmonHub application, which has it's own layer of threading already. I am not trying to send and receive in different threads at the moment so will see were we get to with this..

A few minor modifications have been made to provide compatibility with prototype hardware that has a custom chip select pin. There's also a method to pop one item off the packet queue at one time, needed for compatibility with EmonHub.

I may well upgrade this to the latest version of jgillula/rpi-rfm69 at some point, especially after switching to the standard chip select pin.

### RFM69 Radio interface for the Raspberry Pi
This package provides a Python wrapper of the [LowPowerLabs RFM69 library](https://github.com/LowPowerLab/RFM69) and is largely based on the work of [Eric Trombly](https://github.com/etrombly/RFM69) who ported the library from C.

The package expects to be installed on a Raspberry Pi and depends on the [RPI.GPIO](https://pypi.org/project/RPi.GPIO/) and [spidev](https://pypi.org/project/spidev/) libraries. In addition you need to have an RFM69 radio module directly attached to the Pi. 

For details on how to connect such a module and further information regarding the API check out the [documentation](https://rpi-rfm69.readthedocs.io/).
