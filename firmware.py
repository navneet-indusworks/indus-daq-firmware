# ========================
# Imports and Initialization
# ========================
from machine import Timer, Pin, WDT, reset, disable_irq, enable_irq
from time import sleep
import json
import emonlib_esp32 as emonlib
import wifimgr
import requests
from esp32 import PCNT

# ========================
# Constants
# ========================
VERSION = 1
LED_PIN = 32
CT_SENSOR_PIN = 33
OUTPUT_SIGNAL_PIN = 18
REJECTION_SIGNAL_PIN = 19
WDT_TIMEOUT = 30000
TELEMETRY_MAX_FAILURES = 5

# ========================
# Hardware Setup
# ========================
led = Pin(LED_PIN, Pin.OUT)
wdt = WDT(timeout=WDT_TIMEOUT)

# ========================
# Global Variables
# ========================
site = ''
device_id = ''
api_key = ''
api_secret = ''

telemetry_logging_frequency = 60 # Default is 60 seconds
telemetry_failures = 0  # Tracks how many times telemetry has failed in a row

enable_state_logging = True
ct = None
current = 0.00

output_pulse_counter_function  = None
accumulated_output_pulses = 0
accumulated_unsent_output = 0
output_signal_type = 'NPN'

rejection_pulses_counter_function = None
accumulated_rejection_pulses = 0
accumulated_unsent_rejection = 0
rejection_signal_type = 'NPN'

wlan = None

# ========================
# Helper Functions
# ========================
def check_wifi_connection():
    """Ensure device is connected to Wi-Fi; reconnect if necessary."""
    global wlan, requests
    if wlan is None or not wlan.isconnected():
        print("WiFi connection lost. Attempting to reconnect...")
        wlan = wifimgr.get_connection()
        if wlan is None or not wlan.isconnected():
            print("Failed to reconnect to WiFi")
            return False
        else:
            print("Successfully reconnected to WiFi")
            # Re-import urequests to clear any broken TLS state
            try:
                import sys
                if 'urequests' in sys.modules:
                    del sys.modules['urequests']
                import urequests as requests
            except:
                import requests
            return True
    return True

def check_telemetry_failure_limit():
    global telemetry_failures
    """Check if telemetry has failed too many times and reset if needed."""
    if telemetry_failures >= TELEMETRY_MAX_FAILURES:
        print("[!] Too many telemetry failures. Resetting device...")
        reset()
    return False

def measure_current():
    global current, ct
    current = ct.calc_current_rms(1480)


# ========================
# API Functions
# ========================
def get_configuration():
    """Fetch the device configuration from server."""
    global site, device_id, api_key, api_secret
    try:
        url = f'https://{site}/api/v2/method/indusworks_mes.api.get_device_configuration?device_id={device_id}'
        headers = {'Authorization': f'token {api_key}:{api_secret}'}
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            print(response.text)
            return response.json()
        else:
            print('[!] get_configuration: response not received')
            return None
    except Exception as e:
        print("Error Getting Configuration: ", e)
        return None

def send_telemetry():
    """Send telemetry to server and track failure count."""
    global site, device_id, api_key, api_secret
    global current
    global output_pulse_counter_function, accumulated_unsent_output
    global rejection_pulses_counter_function, accumulated_unsent_rejection
    global telemetry_failures
    global wlan

    # Read current pulse values and store them safely
    state = disable_irq()
    try:
        current_output_count = output_pulse_counter_function.value(0) if output_pulse_counter_function else 0
        current_rejection_count = rejection_pulses_counter_function.value(0) if rejection_pulses_counter_function else 0
        
        # Add current counts to any previously unsent counts
        total_output_to_send = current_output_count + accumulated_unsent_output
        total_rejection_to_send = current_rejection_count + accumulated_unsent_rejection
    finally:
        enable_irq(state)

    try:
        url = f'https://{site}/api/v2/method/indusworks_mes.api.create_telemetry?device_id={device_id}&current={current}&output_signal_count={total_output_to_send}&rejection_signal_count={total_rejection_to_send}'
        headers = {'Authorization': f'token {api_key}:{api_secret}'}
        response = requests.post(url, headers=headers)
        
        if response.status_code == 200:
            print(f"Telemetry sent successfully: Output={total_output_to_send}, Rejection={total_rejection_to_send}")
            # Clear the unsent accumulation on success
            accumulated_unsent_output = 0
            accumulated_unsent_rejection = 0
            telemetry_failures = 0
            return True
        else:
            print(f'[!] create_telemetry: response error {response.status_code}')
            # Save the counts that weren't sent successfully
            accumulated_unsent_output = total_output_to_send
            accumulated_unsent_rejection = total_rejection_to_send
            
            # Check wifi connection if request fails
            if not wlan.isconnected():
                check_wifi_connection()
    except Exception as e:
        import sys
        print("Error Sending Telemetry:")
        sys.print_exception(e)
        
        # Save the counts that weren't sent successfully
        accumulated_unsent_output = total_output_to_send
        accumulated_unsent_rejection = total_rejection_to_send
        
        # This handles network connectivity issues
        if not check_wifi_connection():
            print("No WiFi connection, caching telemetry data")

    # Increment failure count
    telemetry_failures += 1
    return check_telemetry_failure_limit()


def run():
    global site, device_id, api_key, api_secret
    global telemetry_logging_frequency
    global enable_state_logging, current, ct
    global output_pulse_counter_function, output_signal_type
    global rejection_pulses_counter_function, rejection_signal_type
    global wlan

    try:
        wdt.feed()

        # Load credentials and site info from settings.json
        try:
            with open('settings.json', 'r') as file:
                settings = json.load(file)
            site = settings.get('site')
            device_id = settings.get('device_id')
            api_key = settings.get('api_key')
            api_secret = settings.get('api_secret')
        except (OSError, ValueError) as e:
            print(f"Settings load error: {e}")
            reset()

        # Fetch configuration from server
        configuration = get_configuration()
        if not configuration:
            print('No configuration found, resetting the device')
            reset()

        # Setup CT sensor for current monitoring
        enable_state_logging = bool(configuration["data"]["enable_state_logging"])
        if enable_state_logging:
            ct = emonlib.Emonlib()
            ct.current(Pin(CT_SENSOR_PIN), 66.6)
            for _ in range(10):
                current = ct.calc_current_rms(1480)
                print(f'Dummy Value: {current}')
            measure_current_timer = Timer(2)
            measure_current_timer.init(period=1000,mode=Timer.PERIODIC,callback=lambda t: measure_current())

        # Setup output signal pulse counting
        enable_output_signal = bool(configuration["data"]["enable_output_signal"])
        if enable_output_signal:
            output_signal_type = str(configuration["data"]["output_signal_type"])
            output_pin = Pin(OUTPUT_SIGNAL_PIN, Pin.IN, Pin.PULL_UP)
            if output_signal_type == 'NPN':
                rising_action = PCNT.IGNORE
                falling_action = PCNT.INCREMENT
            elif output_signal_type == 'PNP':
                rising_action = PCNT.INCREMENT
                falling_action = PCNT.IGNORE
            output_pulse_counter_function = PCNT(0, pin=output_pin, rising=rising_action, falling=falling_action)
            output_pulse_counter_function.start()

        # Setup rejection signal pulse counting
        enable_rejection_signal = bool(configuration["data"]["enable_rejection_signal"])
        if enable_rejection_signal:
            rejection_signal_type = str(configuration["data"]["rejection_signal_type"])
            rejection_pin = Pin(REJECTION_SIGNAL_PIN, Pin.IN, Pin.PULL_UP)
            if rejection_signal_type == 'NPN':
                rising_action = PCNT.IGNORE
                falling_action = PCNT.INCREMENT
            elif rejection_signal_type == 'PNP':
                rising_action = PCNT.INCREMENT
                falling_action = PCNT.IGNORE
            rejection_pulses_counter_function = PCNT(1, pin=rejection_pin, rising=rising_action, falling=falling_action)
            rejection_pulses_counter_function.start()
        
        # Setup telemetry timer
        telemetry_logging_frequency = int(configuration["data"]["telemetry_logging_frequency"])
        send_telemetry_signal = Timer(1)
        send_telemetry_signal.init(period=telemetry_logging_frequency * 1000,mode=Timer.PERIODIC,callback=lambda t: send_telemetry())

        # Boot diagnostics
        print(f"System initialized:")
        print(f"- Telemetry frequency: {telemetry_logging_frequency} seconds")
        print(f"- Output signal enabled: {enable_output_signal}")
        print(f"- Rejection signal enabled: {enable_rejection_signal}")
        print(f"- State monitoring enabled: {enable_state_logging}")

        # Main loop
        while True:
            wdt.feed()
            current_output_pulses = output_pulse_counter_function.value() if output_pulse_counter_function else 0
            current_rejection_pulses = rejection_pulses_counter_function.value() if rejection_pulses_counter_function else 0
            print(f"Macine Current: {current}, Output Pulses Recieved: {current_output_pulses}, Rejection Pulses Recieved: {current_rejection_pulses}")


    except Exception as e:
        print("Error Running The Main Function: ", e)
        reset()

if __name__ == '__main__':
    run()
