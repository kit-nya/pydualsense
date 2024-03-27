import logging
import os
import sys
from sys import platform

if platform.startswith('Windows') and sys.version_info >= (3, 8):
    os.add_dll_directory(os.getcwd())

import hid
from .enums import (LedOptions, PlayerID, PulseOptions, TriggerModes, Brightness, ConnectionType, BatteryState) # type: ignore
import threading
from .event_system import Event
from copy import deepcopy


logger = logging.getLogger()
FORMAT = '%(asctime)s %(message)s'
logging.basicConfig(format=FORMAT)
logger.setLevel(logging.INFO)

class pydualsense:
    # Bluetooth Checksum seeds
    INPUT_CRC_SEED = b'\xa1'
    OUTPUT_CRC_SEED = b'\xa2'
    # Feature report not implemented (I don't think?)
    FEATURE_CRC_SEED = b'\xa2'

    # Report ID Constants
    INPUT_REPORT_USB = 0x01
    INPUT_REPORT_BT = 0x31
    OUTPUT_REPORT_USB = 0x02
    OUTPUT_REPORT_BT = 0x31

    def __init__(self, verbose: bool = False) -> None:
        """
        initialise the library but dont connect to the controller. call :func:`init() <pydualsense.pydualsense.init>` to connect to the controller

        Args:
            verbose (bool, optional): display verbose out (debug prints of input and output). Defaults to False.
        """
        # TODO: maybe add a init function to not automatically allocate controller when class is declared
        self.verbose = verbose

        if self.verbose:
            logger.setLevel(logging.DEBUG)

        self.leftMotor = 0
        self.rightMotor = 0

        self.last_states = None

        self.register_available_events()

    def register_available_events(self) -> None:
        """
        register all available events that can be used for the controller
        """

        # button events
        self.triangle_pressed = Event()
        self.circle_pressed = Event()
        self.cross_pressed = Event()
        self.square_pressed = Event()

        # dpad events
        # TODO: add a event that sends the pressed key if any key is pressed
        # self.dpad_changed = Event()
        self.dpad_up = Event()
        self.dpad_down = Event()
        self.dpad_left = Event()
        self.dpad_right = Event()

        # joystick
        self.left_joystick_changed = Event()
        self.right_joystick_changed = Event()

        # trigger back buttons
        self.r1_changed = Event()
        self.r2_changed = Event()
        self.r3_changed = Event()

        self.l1_changed = Event()
        self.l2_changed = Event()
        self.l3_changed = Event()

        # misc
        self.ps_pressed = Event()
        self.touch_pressed = Event()
        self.microphone_pressed = Event()
        self.share_pressed = Event()
        self.option_pressed = Event()

        # trackpad touch
        # handles 1 or 2 fingers
        #self.trackpad_frame_reported = Event()

        # gyrometer events
        self.gyro_changed = Event()

        self.accelerometer_changed = Event()

    def init(self) -> None:
        """
        initialize module and device states. Starts the sendReport background thread at the end
        """
        self.device: hid.Device = self.__find_device()
        self.light = DSLight() # control led light of ds
        self.audio = DSAudio() # ds audio setting
        self.triggerL = DSTrigger() # left trigger
        self.triggerR = DSTrigger() # right trigger
        self.state = DSState() # controller states
        # self.conType = self.determineConnectionType() # determine USB or BT connection
        self.battery = DSBattery()
        self.ds_thread = True
        self.report_thread = threading.Thread(target=self.sendReport)
        self.report_thread.start()
        self.states = None

    def determineConnectionType(self) -> ConnectionType:
        """
        Determine the connection type of the controller. eg USB or BT.

        We ask the controller for an input report with a length up to 100 bytes
        and afterwords check the lenght of the received input report.
        The connection type determines the length of the report.

        This way of determining is not pretty but it works..

        Returns:
            ConnectionType: Detected connection type of the controller.
        """
        if self.device['bus_type'] == hid.BusType.USB:
            return ConnectionType.USB
        else:
            return ConnectionType.BT
        dummy_report = self.device.read(100)
        input_report_length = len(dummy_report)

        if input_report_length == 64:
            self.input_report_length = 64
            self.output_report_length = 64
            return ConnectionType.USB
        elif input_report_length == 78:
            self.input_report_length = 78
            self.output_report_length = 78
            self.output_report_seq_id = 0x0
            return ConnectionType.BT

    def close(self) -> None:
        """
        Stops the report thread and closes the HID device
        """
        # TODO: reset trigger effect to default

        self.ds_thread = False
        self.report_thread.join()
        self.device.close()

    def __find_device(self) -> hid.Device:
        """
        find HID dualsense device and open it

        Raises:
            Exception: HIDGuardian detected
            Exception: No device detected

        Returns:
            hid.Device: returns opened controller device
        """
        # TODO: detect connection mode, bluetooth has a bigger write buffer
        # TODO: implement multiple controllers working
        if sys.platform.startswith('win32'):
            import pydualsense.hidguardian as hidguardian
            if hidguardian.check_hide():
                raise Exception('HIDGuardian detected. Delete the controller from HIDGuardian and restart PC to connect to controller')
        detected_device: hid.Device = None
        devices = hid.enumerate()
        for device in devices:
            if device['vendor_id'] == 0x054C and device['product_id'] == 0x0CE6:
                detected_device = device
                break

        if detected_device is None:
            raise Exception('No device detected')
        # 0x054C and device.product_id == 0x0CE6
        if detected_device['bus_type'] == hid.BusType.USB:
            self.conType = ConnectionType.USB
            self.input_report_length = 64
            self.output_report_length = 64
        else:
            self.conType = ConnectionType.BT
            self.input_report_length = 78
            self.output_report_length = 78
            self.output_report_seq_id = 0x0
        dual_sense = hid.Device(path=detected_device['path'])
        return dual_sense

    def add_checksum(self, data: list) -> list:
        from binascii import crc32
        crc32_result = crc32(pydualsense.OUTPUT_CRC_SEED)
        crc32_result = crc32(bytearray(data[:-4]), crc32_result)
        checksum_bytes = crc32_result.to_bytes(4, byteorder='little')
        data[-4:] = checksum_bytes
        return data

    def validate_checksum(self, data: list | bytearray) -> bool:
        from binascii import crc32
        if isinstance(data, list):
            data = bytearray(data)
        crc32_result = crc32(pydualsense.INPUT_CRC_SEED)
        crc32_result = crc32(data[:-4], crc32_result)
        checksum_bytes = crc32_result.to_bytes(4, byteorder='little')
        return checksum_bytes == bytes(data[-4:])

    def setLeftMotor(self, intensity: int) -> None:
        """
        set left motor rumble

        Args:
            intensity (int): rumble intensity

        Raises:
            TypeError: intensity false type
            Exception: intensity out of bounds 0..255
        """
        if not isinstance(intensity, int):
            raise TypeError('left motor intensity needs to be an int')

        if intensity > 255 or intensity < 0:
            raise Exception('maximum intensity is 255')
        self.leftMotor = intensity

    def setRightMotor(self, intensity: int) -> None:
        """
        set right motor rumble

        Args:
            intensity (int): rumble intensity

        Raises:
            TypeError: intensity false type
            Exception: intensity out of bounds 0..255
        """
        if not isinstance(intensity, int):
            raise TypeError('right motor intensity needs to be an int')

        if intensity > 255 or intensity < 0:
            raise Exception('maximum intensity is 255')
        self.rightMotor = intensity

    def sendReport(self) -> None:
        """background thread handling the reading of the device and updating its states
        """
        while self.ds_thread:
            # read data from the input report of the controller
            inReport = self.device.read(self.input_report_length)
            if self.verbose:
                logger.debug(inReport)
            # decrypt the packet and bind the inputs
            self.readInput(inReport)

            # prepare new report for device
            outReport = self.prepareReport()

            # write the report to the device
            self.writeReport(outReport)

    def readInput(self, inReport) -> None:
        if self.conType == ConnectionType.BT:
            # First validate the report and skip if it's invalid
            if not self.validate_checksum(inReport):
                logger.warning('checksum failed')
                return
            # Bluetooth report comes prefixed with a tag byte. The meaning is unclear.
            # Dropping the byte shifts us to the same common report format as USB
            states = list(inReport)[1:]  # convert bytes to list

        else:  # USB
            states = list(inReport)  # convert bytes to list

        # Common report size is USB report size less the report ID (i.e. 62 bytes)
        self.states = states
        # states 0 is always 1
        """
        read the input from the controller and assign the states

        Args:
            inReport (bytearray): read bytearray containing the state of the whole controller
        """
        self.state.LX = states[1] - 128
        self.state.LY = states[2] - 128
        self.state.RX = states[3] - 128
        self.state.RY = states[4] - 128
        self.state.L2 = states[5]
        self.state.R2 = states[6]

        # state 7 always increments -> not used anywhere

        buttonState = states[8]
        self.state.triangle = (buttonState & (1 << 7)) != 0
        self.state.circle = (buttonState & (1 << 6)) != 0
        self.state.cross = (buttonState & (1 << 5)) != 0
        self.state.square = (buttonState & (1 << 4)) != 0

        # dpad
        dpad_state = buttonState & 0x0F
        self.state.setDPadState(dpad_state)

        misc = states[9]
        self.state.R3 = (misc & (1 << 7)) != 0
        self.state.L3 = (misc & (1 << 6)) != 0
        self.state.options = (misc & (1 << 5)) != 0
        self.state.share = (misc & (1 << 4)) != 0
        self.state.R2Btn = (misc & (1 << 3)) != 0
        self.state.L2Btn = (misc & (1 << 2)) != 0
        self.state.R1 = (misc & (1 << 1)) != 0
        self.state.L1 = (misc & (1 << 0)) != 0

        misc2 = states[10]
        self.state.ps = (misc2 & (1 << 0)) != 0
        self.state.touchBtn = (misc2 & 0x02) != 0
        self.state.micBtn = (misc2 & 0x04) != 0

        # trackpad touch
        self.state.trackPadTouch0.ID = inReport[33] & 0x7F
        self.state.trackPadTouch0.isActive = (inReport[33] & 0x80) == 0
        self.state.trackPadTouch0.X = ((inReport[35] & 0x0f) << 8) | (inReport[34])
        self.state.trackPadTouch0.Y = ((inReport[36]) << 4) | ((inReport[35] & 0xf0) >> 4)

        # trackpad touch
        self.state.trackPadTouch1.ID = inReport[37] & 0x7F
        self.state.trackPadTouch1.isActive = (inReport[37] & 0x80) == 0
        self.state.trackPadTouch1.X = ((inReport[39] & 0x0f) << 8) | (inReport[38])
        self.state.trackPadTouch1.Y = ((inReport[40]) << 4) | ((inReport[39] & 0xf0) >> 4)

        # accelerometer
        self.state.accelerometer.X = int.from_bytes(([inReport[16], inReport[17]]), byteorder='little', signed=True)
        self.state.accelerometer.Y = int.from_bytes(([inReport[18], inReport[19]]), byteorder='little', signed=True)
        self.state.accelerometer.Z = int.from_bytes(([inReport[20], inReport[21]]), byteorder='little', signed=True)

        # gyrometer
        self.state.gyro.Pitch = int.from_bytes(([inReport[22], inReport[23]]), byteorder='little', signed=True)
        self.state.gyro.Yaw = int.from_bytes(([inReport[24], inReport[25]]), byteorder='little', signed=True)
        self.state.gyro.Roll = int.from_bytes(([inReport[26], inReport[27]]), byteorder='little', signed=True)

        battery = states[53]
        self.battery.State = BatteryState((battery & 0xF0) >> 4)
        self.battery.Level = min((battery & 0x0F) * 10 + 5, 100)

        # first call we dont have a "last state" so we create if with the first occurence
        if self.last_states is None:
            self.last_states = deepcopy(self.state)
            return

        # send all events if neede
        if self.state.circle != self.last_states.circle:
            self.circle_pressed(self.state.circle)

        if self.state.cross != self.last_states.cross:
            self.cross_pressed(self.state.cross)

        if self.state.triangle != self.last_states.triangle:
            self.triangle_pressed(self.state.triangle)

        if self.state.square != self.last_states.square:
            self.square_pressed(self.state.square)

        if self.state.DpadDown != self.last_states.DpadDown:
            self.dpad_down(self.state.DpadDown)

        if self.state.DpadLeft != self.last_states.DpadLeft:
            self.dpad_left(self.state.DpadLeft)

        if self.state.DpadRight != self.last_states.DpadRight:
            self.dpad_right(self.state.DpadRight)

        if self.state.DpadUp != self.last_states.DpadUp:
            self.dpad_up(self.state.DpadUp)

        if self.state.LX != self.last_states.LX or self.state.LY != self.last_states.LY:
            self.left_joystick_changed(self.state.LX, self.state.LY)

        if self.state.RX != self.last_states.RX or self.state.RY != self.last_states.RY:
            self.right_joystick_changed(self.state.RX, self.state.RY)

        if self.state.R1 != self.last_states.R1:
            self.r1_changed(self.state.R1)

        if self.state.R2 != self.last_states.R2:
            self.r2_changed(self.state.R2)

        if self.state.L1 != self.last_states.L1:
            self.l1_changed(self.state.L1)

        if self.state.L2 != self.last_states.L2:
            self.l2_changed(self.state.L2)

        if self.state.R3 != self.last_states.R3:
            self.r3_changed(self.state.R3)

        if self.state.L3 != self.last_states.L3:
            self.l3_changed(self.state.L3)

        if self.state.ps != self.last_states.ps:
            self.ps_pressed(self.state.ps)

        if self.state.touchBtn != self.last_states.touchBtn:
            self.touch_pressed(self.state.touchBtn)

        if self.state.micBtn != self.last_states.micBtn:
            self.microphone_pressed(self.state.micBtn)

        if self.state.share != self.last_states.share:
            self.share_pressed(self.state.share)

        if self.state.options != self.last_states.options:
            self.option_pressed(self.state.options)

        if self.state.accelerometer.X != self.last_states.accelerometer.X or \
            self.state.accelerometer.Y != self.last_states.accelerometer.Y or \
                self.state.accelerometer.Z != self.last_states.accelerometer.Z:
            self.accelerometer_changed(self.state.accelerometer.X, self.state.accelerometer.Y, self.state.accelerometer.Z)

        if self.state.gyro.Pitch != self.last_states.gyro.Pitch or \
            self.state.gyro.Yaw != self.last_states.gyro.Yaw or \
                self.state.gyro.Roll != self.last_states.gyro.Roll:
            self.gyro_changed(self.state.gyro.Pitch, self.state.gyro.Yaw, self.state.gyro.Roll)

        """
        copy current state into temp object to check next cycle if a change occuret
        and event trigger is needed
        """
        self.last_states = deepcopy(self.state) # copy current state into object to check next time

        # TODO: control mouse with touchpad for fun as DS4Windows

    def writeReport(self, outReport) -> None:
        """
        write the report to the device

        Args:
            outReport (list): report to be written to device
        """
        self.device.write(bytes(outReport))

    def flag1(self, muteMicEnable=False, powerSaveEnable=False, lightBarControl=False, releaseLEDs=False,
              playerIndicatorControl=False):
        output = 0
        output = output | (muteMicEnable << 0)
        output = output | (powerSaveEnable << 1)
        output = output | (lightBarControl << 2)
        output = output | (releaseLEDs << 3)
        output = output | (playerIndicatorControl << 4)
        return output

    def flag2(self, lightBarSetupEnable=False, compatibleVibration=False):
        output = 0
        output = output | (lightBarSetupEnable << 0)
        output = output | (compatibleVibration << 1)
        return output

    def flag0(self, compatibleVibration=False, hapticsSelect=False):
        output = 0
        output = output | (compatibleVibration << 1)
        output = output | (hapticsSelect << 2)
        return output

    def prepareReport(self) -> None:
        """
        prepare the output to be send to the controller

        Returns:
            list: report to send to controller
        """

        outReport = [0] * self.output_report_length  # create empty list with range of output report
        # packet type
        if self.conType == ConnectionType.BT:
            # ReportID
            outReport[0] = pydualsense.OUTPUT_REPORT_BT
            # Sequence Tag
            outReport[1] = (self.output_report_seq_id << 4) | 0x00
            self.output_report_seq_id = (self.output_report_seq_id + 1) % 16
            # Tag. Note: This is just a magic number :(
            outReport[2] = 0x10
        else:
            outReport[0] = pydualsense.OUTPUT_REPORT_USB

        outReportCommon = [0] * 47
        # flags determing what changes this packet will perform
        # 0x01 set the main motors (also requires flag 0x02); setting this by itself will allow rumble to gracefully terminate and then re-enable audio haptics, whereas not setting it will kill the rumble instantly and re-enable audio haptics.
        # 0x02 set the main motors (also requires flag 0x01; without bit 0x01 motors are allowed to time out without re-enabling audio haptics)
        # 0x04 set the right trigger motor
        # 0x08 set the left trigger motor
        # 0x10 modification of audio volume
        # 0x20 toggling of internal speaker while headset is connected
        # 0x40 modification of microphone volume
        # outReportCommon[0] = self.flag0(True, True)
        outReportCommon[0] = 0xFF

        # further flags determining what changes this packet will perform
        # 0x01 toggling microphone LED
        # 0x02 toggling audio/mic mute
        # 0x04 toggling LED strips on the sides of the touchpad
        # 0x08 will actively turn all LEDs off? Convenience flag? (if so, third parties might not support it properly)
        # 0x10 toggling white player indicator LEDs below touchpad
        # 0x20 ???
        # 0x40 adjustment of overall motor/effect power (index 37 - read note on triggers)
        # 0x80 ???
        outReportCommon[1] = 0x1 | 0x2 | 0x4 | 0x10 | 0x40 # [2]
        # Function below should control the flags, but probably makes sense to properly implement
        # rather than just toggling everything on?
        # outReportCommon[1] = self.flag1(True, True, True, False, True)
        outReportCommon[2] = self.rightMotor  # right low freq motor 0-255 # [3]
        outReportCommon[3] = self.leftMotor  # left low freq motor 0-255 # [4]

        # outReport[4] - outReport[7] Audio Reserved

        # set Micrphone LED, setting doesnt effect microphone settings
        outReportCommon[8] = self.audio.microphone_led  # [9]
        # outReportCommon[9] Power save control? Whatever that is...
        outReportCommon[9] = 0x10 if self.audio.microphone_mute is True else 0x00

        # add right trigger mode + parameters to packet
        outReportCommon[10] = self.triggerR.mode.value
        outReportCommon[11] = self.triggerR.forces[0]
        outReportCommon[12] = self.triggerR.forces[1]
        outReportCommon[13] = self.triggerR.forces[2]
        outReportCommon[14] = self.triggerR.forces[3]
        outReportCommon[15] = self.triggerR.forces[4]
        outReportCommon[16] = self.triggerR.forces[5]
        outReportCommon[19] = self.triggerR.forces[6]

        outReportCommon[21] = self.triggerL.mode.value
        outReportCommon[22] = self.triggerL.forces[0]
        outReportCommon[23] = self.triggerL.forces[1]
        outReportCommon[24] = self.triggerL.forces[2]
        outReportCommon[25] = self.triggerL.forces[3]
        outReportCommon[26] = self.triggerL.forces[4]
        outReportCommon[27] = self.triggerL.forces[5]
        outReportCommon[30] = self.triggerL.forces[6]

        outReportCommon[37] = self.flag2(True, True)
        outReportCommon[38] = self.light.ledOption.value
        outReportCommon[41] = self.light.pulseOptions.value
        outReportCommon[42] = self.light.brightness.value
        outReportCommon[43] = self.light.playerNumber.value
        outReportCommon[44] = self.light.TouchpadColor[0]
        outReportCommon[45] = self.light.TouchpadColor[1]
        outReportCommon[46] = self.light.TouchpadColor[2]

        if self.conType == ConnectionType.BT:
            outReport[3:50] = outReportCommon
            outReport = self.add_checksum(outReport)
        else:
            outReport[1:48] = outReportCommon
        if self.verbose:
            logger.debug(outReport)

        return outReport


class DSTouchpad:
    """
    Dualsense Touchpad class. Contains X and Y position of touch and if the touch isActive
    """
    def __init__(self) -> None:
        """
        Class represents the Touchpad of the controller
        """
        self.isActive = False
        self.ID = 0
        self.X = 0
        self.Y = 0


class DSState:

    def __init__(self) -> None:
        """
        All dualsense states (inputs) that can be read. Second method to check if a input is pressed.
        """
        self.square, self.triangle, self.circle, self.cross = False, False, False, False
        self.DpadUp, self.DpadDown, self.DpadLeft, self.DpadRight = False, False, False, False
        self.L1, self.L2, self.L3, self.R1, self.R2, self.R3, self.R2Btn, self.L2Btn = False, False, False, False, False, False, False, False
        self.share, self.options, self.ps, self.touch1, self.touch2, self.touchBtn, self.touchRight, self.touchLeft = False, False, False, False, False, False, False, False
        self.touchFinger1, self.touchFinger2 = False, False
        self.micBtn = False
        self.RX, self.RY, self.LX, self.LY = 128, 128, 128, 128
        self.trackPadTouch0, self.trackPadTouch1 = DSTouchpad(), DSTouchpad()
        self.gyro = DSGyro()
        self.accelerometer = DSAccelerometer()

    def setDPadState(self, dpad_state: int):
        """
        Sets the dpad state variables according to the integers that was read from the controller

        Args:
            dpad_state (int): integer number representing the dpad state
        """
        if dpad_state == 0:
            self.DpadUp = True
            self.DpadDown = False
            self.DpadLeft = False
            self.DpadRight = False
        elif dpad_state == 1:
            self.DpadUp = True
            self.DpadDown = False
            self.DpadLeft = False
            self.DpadRight = True
        elif dpad_state == 2:
            self.DpadUp = False
            self.DpadDown = False
            self.DpadLeft = False
            self.DpadRight = True
        elif dpad_state == 3:
            self.DpadUp = False
            self.DpadDown = True
            self.DpadLeft = False
            self.DpadRight = True
        elif dpad_state == 4:
            self.DpadUp = False
            self.DpadDown = True
            self.DpadLeft = False
            self.DpadRight = False
        elif dpad_state == 5:
            self.DpadUp = False
            self.DpadDown = True
            self.DpadLeft = False
            self.DpadRight = False
        elif dpad_state == 6:
            self.DpadUp = False
            self.DpadDown = False
            self.DpadLeft = True
            self.DpadRight = False
        elif dpad_state == 7:
            self.DpadUp = True
            self.DpadDown = False
            self.DpadLeft = True
            self.DpadRight = False
        else:
            self.DpadUp = False
            self.DpadDown = False
            self.DpadLeft = False
            self.DpadRight = False


class DSLight:
    """
    Represents all features of lights on the controller
    """
    def __init__(self) -> None:
        self.brightness: Brightness = Brightness.low # sets
        self.playerNumber: PlayerID = PlayerID.PLAYER_1
        self.ledOption: LedOptions = LedOptions.Both
        self.pulseOptions: PulseOptions = PulseOptions.Off
        self.TouchpadColor = (0, 0, 255)

    def setLEDOption(self, option: LedOptions):
        """
        Sets the LED Option

        Args:
            option (LedOptions): Led option

        Raises:
            TypeError: LedOption is false type
        """
        if not isinstance(option, LedOptions):
            raise TypeError('Need LEDOption type')
        self.ledOption = option

    def setPulseOption(self, option: PulseOptions):
        """
        Sets the Pulse Option of the LEDs

        Args:
            option (PulseOptions): pulse option of the LEDs

        Raises:
            TypeError: Pulse option is false type
        """
        if not isinstance(option, PulseOptions):
            raise TypeError('Need PulseOption type')
        self.pulseOptions = option

    def setBrightness(self, brightness: Brightness):
        """
        Defines the brightness of the Player LEDs

        Args:
            brightness (Brightness): brightness of LEDS

        Raises:
            TypeError: brightness false type
        """
        if not isinstance(brightness, Brightness):
            raise TypeError('Need Brightness type')
        self.brightness = brightness

    def setPlayerID(self, player: PlayerID):
        """
        Sets the PlayerID of the controller with the choosen LEDs.
        The controller has 4 Player states

        Args:
            player (PlayerID): chosen PlayerID for the Controller

        Raises:
            TypeError: [description]
        """
        if not isinstance(player, PlayerID):
            raise TypeError('Need PlayerID type')
        self.playerNumber = player

    def setColorI(self, r: int, g: int, b: int) -> None:
        """
        Sets the Color around the Touchpad of the controller

        Args:
            r (int): red channel
            g (int): green channel
            b (int): blue channel

        Raises:
            TypeError: color channels have wrong type
            Exception: color channels are out of bounds
        """
        if not isinstance(r, int) or not isinstance(g, int) or not isinstance(b, int):
            raise TypeError('Color parameter need to be int')
        # check if color is out of bounds
        if (r > 255 or g > 255 or b > 255) or (r < 0 or g < 0 or b < 0):
            raise Exception('colors have values from 0 to 255 only')
        self.TouchpadColor = (r, g, b)

    def setColorT(self, color: tuple) -> None:
        """
        Sets the Color around the Touchpad as a tuple

        Args:
            color (tuple): color as tuple

        Raises:
            TypeError: color has wrong type
            Exception: color channels are out of bounds
        """
        if not isinstance(color, tuple):
            raise TypeError('Color type is tuple')
        # unpack for out of bounds check
        r, g, b = map(int, color)
        # check if color is out of bounds
        if (r > 255 or g > 255 or b > 255) or (r < 0 or g < 0 or b < 0):
            raise Exception('colors have values from 0 to 255 only')
        self.TouchpadColor = (r, g, b)


class DSAudio:
    def __init__(self) -> None:
        """
        initialize the limited Audio features of the controller
        """
        self.microphone_mute = 0
        self.microphone_led = 0

    def setMicrophoneLED(self, value):
        """
        Activates or disables the microphone led.
        This doesnt change the mute/unmutes the microphone itself.

        Args:
            value (bool): On or off microphone LED

        Raises:
            Exception: false state for the led
        """
        if not isinstance(value, bool):
            raise TypeError('MicrophoneLED can only be a bool')
        self.microphone_led = value

    def setMicrophoneState(self, state: bool):
        """
        Set the microphone state and also sets the microphone led accordingle

        Args:
            state (bool): desired state of the microphone

        Raises:
            TypeError: state was not a bool
        """

        if not isinstance(state, bool):
            raise TypeError('state needs to be bool')

        self.setMicrophoneLED(state) # set led accordingly
        self.microphone_mute = state


class DSTrigger:
    """
    Dualsense trigger class. Allowes for multiple :class:`TriggerModes <pydualsense.enums.TriggerModes>` and multiple forces

    # TODO: make this interface more userfriendly so a developer knows what he is doing
    """
    def __init__(self) -> None:
        # trigger modes
        self.mode: TriggerModes = TriggerModes.Off

        # force parameters for the triggers
        self.forces = [0 for i in range(7)]

    def setForce(self, forceID: int = 0, force: int = 0):
        """
        Sets the forces of the choosen force parameter

        Args:
            forceID (int, optional): force parameter. Defaults to 0.
            force (int, optional): applied force to the parameter. Defaults to 0.

        Raises:
            TypeError: wrong type of forceID or force
            Exception: choosen a false force parameter
        """
        if not isinstance(forceID, int) or not isinstance(force, int):
            raise TypeError('forceID and force needs to be type int')

        if forceID > 6 or forceID < 0:
            raise Exception('only 7 parameters available')

        self.forces[forceID] = force

    def setMode(self, mode: TriggerModes):
        """
        Set the Mode for the Trigger

        Args:
            mode (TriggerModes): Trigger mode

        Raises:
            TypeError: false Trigger mode type
        """
        if not isinstance(mode, TriggerModes):
            raise TypeError('Trigger mode parameter needs to be of type `TriggerModes`')

        self.mode = mode


class DSGyro:
    """
    Class representing the Gyro2 of the controller
    """
    def __init__(self) -> None:
        self.Pitch = 0
        self.Yaw = 0
        self.Roll = 0


class DSAccelerometer:
    """
    Class representing the Accelerometer of the controller
    """

    def __init__(self) -> None:
        self.X = 0
        self.Y = 0
        self.Z = 0

class DSBattery:
    """
    Class representing the Battery of the controller
    """
    def __init__(self) -> None:
        self.State = BatteryState.POWER_SUPPLY_STATUS_UNKNOWN
        self.Level = 0
