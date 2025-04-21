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
current = 0.00

output_pulses = 0
accumulated_output_counter = 0
pending_output_counter = 0
output_signal_threshold = 0
output_signal_type = 'NPN'

rejection_pulses = 0
accumulated_rejection_counter = 0
pending_rejection_counter = 0
rejection_signal_threshold = 0
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

def update_output_counter(arg):
    global accumulated_output_counter
    accumulated_output_counter += 1

def update_rejection_counter(arg):
    global accumulated_rejection_counter
    accumulated_rejection_counter += 1

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
    global accumulated_output_counter, pending_output_counter
    global accumulated_rejection_counter, pending_rejection_counter
    global telemetry_failures
    global wlan

    state = disable_irq()
    try:
        pending_output_counter = accumulated_output_counter
        accumulated_output_counter = 0
        pending_rejection_counter = accumulated_rejection_counter
        accumulated_rejection_counter = 0
    finally:
        enable_irq(state)

    try:
        url = f'https://{site}/api/v2/method/indusworks_mes.api.create_telemetry?device_id={device_id}&current={current}&output_signal_count={pending_output_counter}&rejection_signal_count={pending_rejection_counter}'
        headers = {'Authorization': f'token {api_key}:{api_secret}'}
        response = requests.post(url, headers=headers)
        
        if response.status_code == 200:
            print(f"Telemetry sent successfully: Output={pending_output_counter}, Rejection={pending_rejection_counter}")
            telemetry_failures = 0
            return True
        else:
            print('[!] create_telemetry: response not received')
            # Only check wifi if request fails with a status code issue
            if not wlan.isconnected():
                check_wifi_connection()
    except Exception as e:
        import sys
        print("Error Sending Telemetry:")
        sys.print_exception(e)
        print("Exception:", e)
        
        # This handles network connectivity issues
        if not check_wifi_connection():
            print("No WiFi connection, caching telemetry data")

    # On failure, restore counts and increment failure count
    state = disable_irq()
    try:
        accumulated_output_counter += pending_output_counter
        accumulated_rejection_counter += pending_rejection_counter
    finally:
        enable_irq(state)
    telemetry_failures += 1
    return check_telemetry_failure_limit()


def run():
    global site, device_id, api_key, api_secret
    global telemetry_logging_frequency
    global enable_state_logging, current
    global output_pulses, output_signal_threshold, output_signal_type
    global accumulated_output_counter, pending_output_counter
    global rejection_pulses, rejection_signal_threshold, rejection_signal_type
    global accumulated_rejection_counter, pending_rejection_counter
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

        # Setup output signal pulse counting
        enable_output_signal = bool(configuration["data"]["enable_output_signal"])
        if enable_output_signal:
            output_signal_threshold = int(configuration["data"]["output_signal_threshold"])
            output_signal_type = str(configuration["data"]["output_signal_type"])
            output_pin = Pin(OUTPUT_SIGNAL_PIN, Pin.IN, Pin.PULL_UP)
            if output_signal_type == 'NPN':
                rising_action = PCNT.IGNORE
                falling_action = PCNT.INCREMENT
            elif output_signal_type == 'PNP':
                rising_action = PCNT.INCREMENT
                falling_action = PCNT.IGNORE
            output_pulses = PCNT(0, pin=output_pin, rising=rising_action, falling=falling_action, maximum=output_signal_threshold)
            output_pulses.irq(handler=update_output_counter, trigger=PCNT.IRQ_MAXIMUM)
            output_pulses.start()

        # Setup rejection signal pulse counting
        enable_rejection_signal = bool(configuration["data"]["enable_rejection_signal"])
        if enable_rejection_signal:
            rejection_signal_threshold = int(configuration["data"]["rejection_signal_threshold"])
            rejection_signal_type = str(configuration["data"]["rejection_signal_type"])
            rejection_pin = Pin(REJECTION_SIGNAL_PIN, Pin.IN, Pin.PULL_UP)
            if rejection_signal_type == 'NPN':
                rising_action = PCNT.IGNORE
                falling_action = PCNT.INCREMENT
            elif rejection_signal_type == 'PNP':
                rising_action = PCNT.INCREMENT
                falling_action = PCNT.IGNORE
            rejection_pulses = PCNT(1, pin=rejection_pin, rising=rising_action, falling=falling_action, maximum=rejection_signal_threshold)
            rejection_pulses.irq(handler=update_rejection_counter, trigger=PCNT.IRQ_MAXIMUM)
            rejection_pulses.start()
        
        # Setup telemetry timer
        telemetry_logging_frequency = int(configuration["data"]["telemetry_logging_frequency"])
        send_telemetry_signal = Timer(1)
        send_telemetry_signal.init(period=telemetry_logging_frequency * 1000,mode=Timer.PERIODIC,callback=lambda t: send_telemetry())

        # Boot diagnostics
        print(f"System initialized:")
        print(f"- Telemetry frequency: {telemetry_logging_frequency} seconds")
        print(f"- Output signal enabled: {enable_output_signal}, threshold: {output_signal_threshold}")
        print(f"- Rejection signal enabled: {enable_rejection_signal}, threshold: {rejection_signal_threshold}")
        print(f"- State monitoring enabled: {enable_state_logging}")

        # Main loop
        while True:
            wdt.feed()
            if enable_state_logging:
                current = ct.calc_current_rms(1480)
            print(f"Current: {current}, Output Counter: {accumulated_output_counter}, Rejection Counter: {accumulated_rejection_counter}")

    except Exception as e:
        print("Error Running The Main Function: ", e)
        reset()

if __name__ == '__main__':
    run()
