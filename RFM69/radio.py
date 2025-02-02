import sys, time, logging
from datetime import datetime
import logging
import spidev
import RPi.GPIO as GPIO
from .registers import *
from .packet import Packet
from .config import get_config

class Radio(object):

    def __init__(self, freqBand, nodeID, networkID=100, **kwargs):
        """RFM69 Radio interface for the Raspberry PI.

        An RFM69 module is expected to be connected to the SPI interface of the Raspberry Pi. The class is as a context manager so you can instantiate it using the 'with' keyword.

        Args: 
            freqBand: Frequency band of radio - 315MHz, 868Mhz, 433MHz or 915MHz.
            nodeID (int): The node ID of this device.
            networkID (int): The network ID

        Keyword Args:
            auto_acknowledge (bool): Automatically send acknowledgements
            isHighPower (bool): Is this a high power radio model
            power (int): Power level - a percentage in range 10 to 100.
            interruptPin (int): Pin number of interrupt pin. This is a pin index not a GPIO number.
            resetPin (int): Pin number of reset pin. This is a pin index not a GPIO number.
            spiBus (int): SPI bus number.
            spiDevice (int): SPI device number.
            promiscuousMode (bool): Listen to all messages not just those addressed to this node ID.
            encryptionKey (str): 16 character encryption key.
            verbose (bool): Verbose mode - Activates logging to console.

        """
        self.logger = None
        if kwargs.get('verbose', False):
            self.logger = self._init_log()

        self.auto_acknowledge = kwargs.get('autoAcknowledge', True)
        self.isRFM69HW = kwargs.get('isHighPower', True)
        self.intPin = kwargs.get('interruptPin', 18)
        self.rstPin = kwargs.get('resetPin', 29)
        self.selPin = kwargs.get('selPin', 16)
        self.spiBus = kwargs.get('spiBus', 0)
        self.spiDevice = kwargs.get('spiDevice', 0)
        self.promiscuousMode = kwargs.get('promiscuousMode', 0)
        
        self.intLock = False
        self.sendLock = False
        self.mode = ""
        self.mode_name = ""
        
        # ListenMode members
        self._isHighSpeed = True
        self._encryptKey = None
        self.listenModeSetDurations(DEFAULT_LISTEN_RX_US, DEFAULT_LISTEN_IDLE_US)
        
        self.sendSleepTime = 0.05

        # 
        self.packets = []
        self.acks = {}
        #
        #         

        self._init_spi()
        self._init_gpio()
        self.init_success = self._initialize(freqBand, nodeID, networkID)
        if self.init_success:
            self._encrypt(kwargs.get('encryptionKey', 0))
            self.set_power_level(kwargs.get('power', 70))


    def _initialize(self, freqBand, nodeID, networkID):
        if not self._reset_radio(): return False
            
        self._set_config(get_config(freqBand, networkID))
        self._setHighPower(self.isRFM69HW)        
        # Wait for ModeReady
        start = time.time()
        while (self._readReg(REG_IRQFLAGS1) & RF_IRQFLAGS1_MODEREADY) == 0x00:
            if time.time() - start > 1.0: 
                return False

        self.address = nodeID
        self._freqBand = freqBand
        self._networkID = networkID
        self._init_interrupt()

        return True
        
    def _init_gpio(self):
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(self.intPin, GPIO.IN)
        if self.rstPin:
            GPIO.setup(self.rstPin, GPIO.OUT)
        GPIO.setup(self.selPin, GPIO.OUT)

    def _init_spi(self):
        #initialize SPI
        self.spi = spidev.SpiDev()
        self.spi.open(self.spiBus, self.spiDevice)
        self.spi.max_speed_hz = 4000000
        self.spi.no_cs = True

    def select(self):
        GPIO.output(self.selPin, GPIO.LOW)

    def unselect(self):
        GPIO.output(self.selPin, GPIO.HIGH)

    def _reset_radio(self):
        # Hard reset the RFM module
        if self.rstPin:
            GPIO.output(self.rstPin, GPIO.HIGH)
            time.sleep(0.3)
            GPIO.output(self.rstPin, GPIO.LOW)
            time.sleep(0.3)
        #verify chip is syncing?
        start = time.time()
        while self._readReg(REG_SYNCVALUE1) != 0xAA:
            self._writeReg(REG_SYNCVALUE1, 0xAA)
            if time.time() - start > 0.1:
                return False
        start = time.time()
        while self._readReg(REG_SYNCVALUE1) != 0x55:
            self._writeReg(REG_SYNCVALUE1, 0x55)
            if time.time() - start > 0.1:
                return False
        return True

    def _set_config(self, config):
        for value in config.values():
            self._writeReg(value[0], value[1])

    def _init_interrupt(self):
        GPIO.remove_event_detect(self.intPin)
        GPIO.add_event_detect(self.intPin, GPIO.RISING, callback=self._interruptHandler)


    # 
    # End of Init
    # 

    def __enter__(self):
        """When the context begins"""
        self.read_temperature()
        self.calibrate_radio()
        self.begin_receive()
        return self

    def __exit__(self, *args):
        """When context exits (including when the script is terminated)"""
        self._shutdown()     
       
    def set_frequency(self, FRF):
        """Set the radio frequency"""
        self._writeReg(REG_FRFMSB, FRF >> 16)
        self._writeReg(REG_FRFMID, FRF >> 8)
        self._writeReg(REG_FRFLSB, FRF)

    def sleep(self):
        """Put the radio into sleep mode"""
        self._setMode(RF69_MODE_SLEEP)

    def set_network(self, network_id):
        """Set the network ID (sync)
        
        Args:
            network_id (int): Value between 1 and 254.

        """
        assert type(network_id) == int
        assert network_id > 0 and network_id < 255
        self._writeReg(REG_SYNCVALUE2, network_id)

    def set_power_level(self, percent):
        """Set the transmit power level
        
        Args:
            percent (int): Value between 0 and 100.

        """
        assert type(percent) == int
        self.powerLevel = int( round(31 * (percent / 100)))
        self._writeReg(REG_PALEVEL, (self._readReg(REG_PALEVEL) & 0xE0) | self.powerLevel)


    def _send(self, toAddress, buff = "", requestACK = False):
        self._writeReg(REG_PACKETCONFIG2, (self._readReg(REG_PACKETCONFIG2) & 0xFB) | RF_PACKET2_RXRESTART)
        now = time.time()
        while (not self._canSend()) and time.time() - now < RF69_CSMA_LIMIT_S:
            self.has_received_packet()
        self._sendFrame(toAddress, buff, requestACK, False)


    def broadcast(self, buff = ""):
        """Broadcast a message to network i.e. sends to node 255 with no ACK request.

        Args:
            buff (str): Message buffer to send 

        """

        broadcastAddress = 255
        self.send(broadcastAddress, buff, require_ack=False)

    def send(self, toAddress, buff = "", **kwargs):
        """Send a message
        
        Args:
            toAddress (int): Recipient node's ID
            buff (str): Message buffer to send 
        
        Keyword Args:
            attempts (int): Number of attempts
            wait (int): Milliseconds to wait for acknowledgement
            require_ack(bool): Require Acknowledgement. If Attempts > 1 this is auto set to True.
        Returns:
            bool: If acknowledgement received or None is no acknowledgement requested
        
        """

        attempts = kwargs.get('attempts', 3)
        wait_time = kwargs.get('wait', 50)
        require_ack = kwargs.get('require_ack', True)
        if attempts > 1:
            require_ack = True

        for _ in range(0, attempts):
            self._send(toAddress, buff, attempts>0 )

            if not require_ack:
                return None

            sentTime = time.time()
            while (time.time() - sentTime) * 1000 < wait_time:
                self._debug("Waiting line 203")
                time.sleep(.05)
                if self._ACKReceived(toAddress):
                    return True

        return False

    def read_temperature(self, calFactor=0):
        """Read the temperature of the radios CMOS chip.
        
        Args:
            calFactor: Additional correction to corrects the slope, rising temp = rising val

        Returns:
            int: Temperature in centigrade
        """
        self._setMode(RF69_MODE_STANDBY)
        self._writeReg(REG_TEMP1, RF_TEMP1_MEAS_START)
        while self._readReg(REG_TEMP1) & RF_TEMP1_MEAS_RUNNING:
            pass
        # COURSE_TEMP_COEF puts reading in the ballpark, user can add additional correction
        #'complement'corrects the slope, rising temp = rising val
        return (int(~self._readReg(REG_TEMP2)) * -1) + COURSE_TEMP_COEF + calFactor


    def calibrate_radio(self):
        """Calibrate the internal RC oscillator for use in wide temperature variations.
        
        See RFM69 datasheet section [4.3.5. RC Timer Accuracy] for more information.
        """
        self._writeReg(REG_OSC1, RF_OSC1_RCCAL_START)
        while self._readReg(REG_OSC1) & RF_OSC1_RCCAL_DONE == 0x00:
            pass

    def read_registers(self):
        """Get all register values.

        Returns:
            list: Register values
        """
        results = []
        for address in range(1, 0x50):
            results.append([str(hex(address)), str(bin(self._readReg(address)))])
        return results

    def begin_receive(self):
        """Begin listening for packets"""
        while self.intLock:
            time.sleep(.1)

        if (self._readReg(REG_IRQFLAGS2) & RF_IRQFLAGS2_PAYLOADREADY):
            # avoid RX deadlocks
            self._writeReg(REG_PACKETCONFIG2, (self._readReg(REG_PACKETCONFIG2) & 0xFB) | RF_PACKET2_RXRESTART)
        #set DIO0 to "PAYLOADREADY" in receive mode
        self._writeReg(REG_DIOMAPPING1, RF_DIOMAPPING1_DIO0_01)
        self._setMode(RF69_MODE_RX)

    def has_received_packet(self):
        """Check if packet received

        Returns:
            bool: True if packet has been received

        """
        return len(self.packets) > 0

    def get_packets(self):
        """Get newly received packets.

        Returns:
            list: Returns a list of RFM69.Packet objects.
        """
        # Create packet
        packets = list(self.packets)
        self.packets = []
        return packets

    def get_packet(self):
        """Get newly received packet.

        Returns:
            list: Returns a single RFM69.Packet.
        """
        if len(self.packets):
            return self.packets.pop(0)
        else:
            return False
   
    def send_ack(self, toAddress, buff = ""):
        """Send an acknowledgemet packet

        Args: 
            toAddress (int): Recipient node's ID

        """
        while not self._canSend():
            self.has_received_packet()
        self._sendFrame(toAddress, buff, False, True)


    # 
    # Internal functions
    # 

    def _setMode(self, newMode):
        if newMode == self.mode:
            return
        if newMode == RF69_MODE_TX:
            self.mode_name = "TX"
            self._writeReg(REG_OPMODE, (self._readReg(REG_OPMODE) & 0xE3) | RF_OPMODE_TRANSMITTER)
            if self.isRFM69HW:
                self._setHighPowerRegs(True)
        elif newMode == RF69_MODE_RX:
            self.mode_name = "RX"
            self._writeReg(REG_OPMODE, (self._readReg(REG_OPMODE) & 0xE3) | RF_OPMODE_RECEIVER)
            if self.isRFM69HW:
                self._setHighPowerRegs(False)
        elif newMode == RF69_MODE_SYNTH:
            self.mode_name = "Synth"
            self._writeReg(REG_OPMODE, (self._readReg(REG_OPMODE) & 0xE3) | RF_OPMODE_SYNTHESIZER)
        elif newMode == RF69_MODE_STANDBY:
            self.mode_name = "Standby"
            self._writeReg(REG_OPMODE, (self._readReg(REG_OPMODE) & 0xE3) | RF_OPMODE_STANDBY)
        elif newMode == RF69_MODE_SLEEP:
            self.mode_name = "Sleep"
            self._writeReg(REG_OPMODE, (self._readReg(REG_OPMODE) & 0xE3) | RF_OPMODE_SLEEP)
        else:
            self.mode_name = "Unknown"
            return
        # we are using packet mode, so this check is not really needed
        # but waiting for mode ready is necessary when going from sleep because the FIFO may not be immediately available from previous mode
        while self.mode == RF69_MODE_SLEEP and self._readReg(REG_IRQFLAGS1) & RF_IRQFLAGS1_MODEREADY == 0x00:
            pass
        self.mode = newMode

    def _setAddress(self, addr):
        self.address = addr
        self._writeReg(REG_NODEADRS, self.address)

    def _canSend(self):
        if self.mode == RF69_MODE_STANDBY:
            self.begin_receive()
            return True
        #if signal stronger than -100dBm is detected assume channel activity - removed self.PAYLOADLEN == 0 and
        elif self.mode == RF69_MODE_RX and self._readRSSI() < CSMA_LIMIT:
            self._setMode(RF69_MODE_STANDBY)
            return True
        return False

    def _ACKReceived(self, fromNodeID):
        if fromNodeID in self.acks:
            self.acks.pop(fromNodeID, None)
            return True
        return False
        # if self.has_received_packet():
        #     return (self.SENDERID == fromNodeID or fromNodeID == RF69_BROADCAST_ADDR) and self.ACK_RECEIVED
        # return False

    

    def _sendFrame(self, toAddress, buff, requestACK, sendACK):
        #turn off receiver to prevent reception while filling fifo
        self._setMode(RF69_MODE_STANDBY)
        #wait for modeReady
        while (self._readReg(REG_IRQFLAGS1) & RF_IRQFLAGS1_MODEREADY) == 0x00:
            pass
        # DIO0 is "Packet Sent"
        self._writeReg(REG_DIOMAPPING1, RF_DIOMAPPING1_DIO0_00)

        if (len(buff) > RF69_MAX_DATA_LEN):
            buff = buff[0:RF69_MAX_DATA_LEN]

        ack = 0
        if sendACK:
            ack = 0x80
        elif requestACK:
            ack = 0x40
        self.select()
        if isinstance(buff, str):
            self.spi.xfer2([REG_FIFO | 0x80, len(buff) + 3, toAddress, self.address, ack] + [int(ord(i)) for i in list(buff)])
        else:
            self.spi.xfer2([REG_FIFO | 0x80, len(buff) + 3, toAddress, self.address, ack] + buff)
        self.unselect()

        self.sendLock = True
        self._setMode(RF69_MODE_TX)
        while ((self._readReg(REG_IRQFLAGS2) & RF_IRQFLAGS2_PACKETSENT) == 0x00):
            time.sleep(0.01)
            pass # make sure packet is sent before putting more into the FIFO
        
        self._setMode(RF69_MODE_RX) # or should this be RF69_MODE_STANDBY?

    def _readRSSI(self, forceTrigger = False):
        rssi = 0
        if forceTrigger:
            self._writeReg(REG_RSSICONFIG, RF_RSSI_START)
            while self._readReg(REG_RSSICONFIG) & RF_RSSI_DONE == 0x00:
                pass
        rssi = self._readReg(REG_RSSIVALUE) * -1
        rssi = rssi >> 1
        return rssi

    def _encrypt(self, key):
        self._setMode(RF69_MODE_STANDBY)
        if key != 0 and len(key) == 16:
            self._encryptKey = key
            self.select()
            self.spi.xfer([REG_AESKEY1 | 0x80] + [int(ord(i)) for i in list(key)])
            self.unselect()
            self._writeReg(REG_PACKETCONFIG2,(self._readReg(REG_PACKETCONFIG2) & 0xFE) | RF_PACKET2_AES_ON)
        else:
            self._encryptKey = None
            self._writeReg(REG_PACKETCONFIG2,(self._readReg(REG_PACKETCONFIG2) & 0xFE) | RF_PACKET2_AES_OFF)

    def _readReg(self, addr):
        self.select()
        regval = self.spi.xfer([addr & 0x7F, 0])[1]
        self.unselect()
        return regval

    def _writeReg(self, addr, value):
        self.select()
        self.spi.xfer([addr | 0x80, value])
        self.unselect()

    def _promiscuous(self, onOff):
        self.promiscuousMode = onOff

    def _setHighPower(self, onOff):
        if onOff:
            self._writeReg(REG_OCP, RF_OCP_OFF)
            #enable P1 & P2 amplifier stages
            self._writeReg(REG_PALEVEL, (self._readReg(REG_PALEVEL) & 0x1F) | RF_PALEVEL_PA1_ON | RF_PALEVEL_PA2_ON)
        else:
            self._writeReg(REG_OCP, RF_OCP_ON)
            #enable P0 only
            self._writeReg(REG_PALEVEL, RF_PALEVEL_PA0_ON | RF_PALEVEL_PA1_OFF | RF_PALEVEL_PA2_OFF | powerLevel)

    def _setHighPowerRegs(self, onOff):
        if onOff:
            self._writeReg(REG_TESTPA1, 0x5D)
            self._writeReg(REG_TESTPA2, 0x7C)
        else:
            self._writeReg(REG_TESTPA1, 0x55)
            self._writeReg(REG_TESTPA2, 0x70)

    def _shutdown(self):
        """Shutdown the radio.

        Puts the radio to sleep and cleans up the GPIO connections.
        """
        self._setHighPower(False)
        self.sleep()
        GPIO.cleanup()

    def __str__(self):
        return "Radio RFM69"

    def __repr__(self):
        return "Radio()"

    def _init_log(self):
        logging.basicConfig(level=logging.DEBUG)
        logger = logging.getLogger(__name__)
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.propagate = False
        return logger

    def _debug(self, *args):
        if self.logger is not None:
             self.logger.debug(*args)
      
    def _error(self, *args):
        if self.logger is not None:
             self.logger.error(*args)
 
    # 
    # Radio interrupt handler
    # 

    def _interruptHandler(self, pin):
        self.intLock = True
        self.sendLock = False

        if self.mode == RF69_MODE_RX and self._readReg(REG_IRQFLAGS2) & RF_IRQFLAGS2_PAYLOADREADY:
            self._setMode(RF69_MODE_STANDBY)
        
            self.select()
            payload_length, target_id, sender_id, CTLbyte = self.spi.xfer2([REG_FIFO & 0x7f,0,0,0,0])[1:]
            self.unselect()
        
            if payload_length > 66:
                payload_length = 66

            if not (self.promiscuousMode or target_id == self.address or target_id == RF69_BROADCAST_ADDR):
                self._debug("Ignore Interrupt")
                self.intLock = False
                self.begin_receive()
                return

            data_length = payload_length - 3
            ack_received  = bool(CTLbyte & 0x80)
            ack_requested = bool(CTLbyte & 0x40)
            self.select()
            data = self.spi.xfer2([REG_FIFO & 0x7f] + [0 for i in range(0, data_length)])[1:]
            self.unselect()
            rssi = self._readRSSI()

            if ack_received:
                self._debug("Incoming ack")
                self._debug(sender_id)
                # Record acknowledgement
                self.acks.setdefault(sender_id, 1)
         
            elif ack_requested:
                self._debug("replying to ack request")
            else:
                self._debug("Other ??")

            # When message received
            if not ack_received:
                self._debug("Incoming data packet")
                self.packets.append(
                    Packet(int(target_id), int(sender_id), int(rssi), list(data))
                )

            # Send acknowledgement if needed
            if ack_requested and self.auto_acknowledge:
                self.intLock = False
                self.send_ack(sender_id)
             
        self.intLock = False
        self.begin_receive()


    # 
    # ListenMode functions
    # 

    def _reinitRadio(self):
        if (not self._initialize(self._freqBand, self.address, self._networkID)):
            return False
        if (self._encryptKey):
            self._encrypt(self._encryptKey); # Restore the encryption key if necessary
        if (self._isHighSpeed):
            self._writeReg(REG_LNA, (self._readReg(REG_LNA) & ~0x3) | RF_LNA_GAINSELECT_AUTO)
        return True

    def _getUsForResolution(self, resolution):
        if resolution == RF_LISTEN1_RESOL_RX_64 or resolution == RF_LISTEN1_RESOL_IDLE_64:
            return 64
        elif resolution == RF_LISTEN1_RESOL_RX_4100 or resolution == RF_LISTEN1_RESOL_IDLE_4100:
            return 4100
        elif resolution == RF_LISTEN1_RESOL_RX_262000 or resolution == RF_LISTEN1_RESOL_IDLE_262000:
            return 262000
        else:
            return 0
                
    def _getCoefForResolution(self, resolution, duration):
        resolDuration = self._getUsForResolution(resolution)
        result = int(duration / resolDuration)
        # If the next-higher coefficient is closer, use that
        if (abs(duration - ((result + 1) * resolDuration)) < abs(duration - (result * resolDuration))):
            return result + 1
        return result
        
    def listenModeHighSpeed(self, highSpeed):
        self._isHighSpeed = highSpeed

    def _chooseResolutionAndCoef(self, resolutions, duration):
        for resolution in resolutions:
            coef = self._getCoefForResolution(resolution, duration)
            if (coef <= 255):
                coefOut = coef
                resolOut = resolution
                return (resolOut, coefOut)
        # out of range
        return (None, None)
                
    def listenModeSetDurations(self, rxDuration, idleDuration):
        rxResolutions = [ RF_LISTEN1_RESOL_RX_64, RF_LISTEN1_RESOL_RX_4100, RF_LISTEN1_RESOL_RX_262000, 0 ]
        idleResolutions = [ RF_LISTEN1_RESOL_IDLE_64, RF_LISTEN1_RESOL_IDLE_4100, RF_LISTEN1_RESOL_IDLE_262000, 0 ]

        (resolOut, coefOut) = self._chooseResolutionAndCoef(rxResolutions, rxDuration)
        if(resolOut and coefOut):
            self._rxListenResolution = resolOut
            self._rxListenCoef = coefOut
        else:
            return (None, None)
        
        (resolOut, coefOut) = self._chooseResolutionAndCoef(idleResolutions, idleDuration)
        if(resolOut and coefOut):
            self._idleListenResolution = resolOut
            self._idleListenCoef = coefOut
        else:
            return (None, None)
        
        rxDuration = self._getUsForResolution(self._rxListenResolution) * self._rxListenCoef
        idleDuration = self._getUsForResolution(self._idleListenResolution) * self._idleListenCoef
        self._listenCycleDurationUs = rxDuration + idleDuration
        return (rxDuration, idleDuration)
        
    def listenModeGetDurations(self):
        rxDuration = self._getUsForResolution(self._rxListenResolution) * self._rxListenCoef
        idleDuration = self._getUsForResolution(self._idleListenResolution) * self._idleListenCoef
        return (rxDuration, idleDuration)
        
    def listenModeApplyHighSpeedSettings(self):
        if (not self._isHighSpeed): return
        self._writeReg(REG_BITRATEMSB, RF_BITRATEMSB_200000)
        self._writeReg(REG_BITRATELSB, RF_BITRATELSB_200000)
        self._writeReg(REG_FDEVMSB, RF_FDEVMSB_100000)
        self._writeReg(REG_FDEVLSB, RF_FDEVLSB_100000)
        self._writeReg( REG_RXBW, RF_RXBW_DCCFREQ_000 | RF_RXBW_MANT_20 | RF_RXBW_EXP_0 )


    def listenModeSendBurst(self, toAddress, buff):
        """Send a message to nodes in listen mode as a burst
        
        Args:
            toAddress (int): Recipient node's ID
            buff (str): Message buffer to send 
        
        """
        GPIO.remove_event_detect(self.intPin) #        detachInterrupt(_interruptNum)
        self._setMode(RF69_MODE_STANDBY)
        self._writeReg(REG_PACKETCONFIG1, RF_PACKET1_FORMAT_VARIABLE | RF_PACKET1_DCFREE_WHITENING | RF_PACKET1_CRC_ON | RF_PACKET1_CRCAUTOCLEAR_ON )
        self._writeReg(REG_PACKETCONFIG2, RF_PACKET2_RXRESTARTDELAY_NONE | RF_PACKET2_AUTORXRESTART_ON | RF_PACKET2_AES_OFF)
        self._writeReg(REG_SYNCVALUE1, 0x5A)
        self._writeReg(REG_SYNCVALUE2, 0x5A)
        self.listenModeApplyHighSpeedSettings()
        self._writeReg(REG_FRFMSB, self._readReg(REG_FRFMSB) + 1)
        self._writeReg(REG_FRFLSB, self._readReg(REG_FRFLSB))      # MUST write to LSB to affect change!
        
        cycleDurationMs = int(self._listenCycleDurationUs / 1000)
        timeRemaining = int(cycleDurationMs)

        self._setMode(RF69_MODE_TX)
        numSent = 0
        startTime = int(time.time() * 1000) #millis()

        while(timeRemaining > 0):
            if isinstance(buff, str):
                self.spi.xfer2([REG_FIFO | 0x80, len(buff) + 4, toAddress, self.address, timeRemaining & 0xFF, (timeRemaining >> 8) & 0xFF] + [int(ord(i)) for i in list(buff)])
            else:
                self.spi.xfer2([REG_FIFO | 0x80, len(buff) + 4, toAddress, self.address, timeRemaining & 0xFF, (timeRemaining >> 8) & 0xFF] + buff)
            
            while ((self._readReg(REG_IRQFLAGS2) & RF_IRQFLAGS2_FIFONOTEMPTY) != 0x00):
                pass # make sure packet is sent before putting more into the FIFO
            timeRemaining = cycleDurationMs - (int(time.time()*1000) - startTime)

        self._setMode(RF69_MODE_STANDBY)
        self._reinitRadio()
