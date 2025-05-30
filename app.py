import eventlet
eventlet.monkey_patch()  # ← Apply first
import time
import pymcprotocol
import eventlet
from flask import Flask, render_template, Blueprint
from flask_socketio import SocketIO
from datetime import datetime, timedelta
import pytz
import logging
import os
from logging.handlers import RotatingFileHandler
import sqlite3
import json

# Log settings
log_dir = 'logs'
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# Logger configuration
logger = logging.getLogger('slap1cast')
logger.setLevel(logging.INFO)

# File handler with log rotation
file_handler = RotatingFileHandler(
    os.path.join(log_dir, 'slap1cast.log'),
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5
)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s'
))
logger.addHandler(file_handler)

# Console output
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s'
))
logger.addHandler(console_handler)

# Define the subpath for this application
SUBPATH = "/slap1cast"  # Change this for different SLAP instances

# Define cycle time
CYCLE_TIME = 150

# Flask & SocketIO configuration
app = Flask(__name__, static_url_path=f'{SUBPATH}/static')
app.config['APPLICATION_ROOT'] = SUBPATH
socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*", path=f"{SUBPATH}/socket.io")

# Create a blueprint for this application
slap_bp = Blueprint('slap1', __name__, url_prefix=SUBPATH)

# PLC connection information
PLC_IP_1A = "192.168.150.22"  # SLAP1A
PLC_IP_1B = "192.168.150.24"  # SLAP1B
PLC_PORT = 5020
MAX_CONNECTION_RETRIES = 5  # Maximum reconnection attempts
RETRY_INTERVAL = 3  # Reconnection interval (seconds)

# Database configuration
DB_FILE = 'slap1cast_data.db'

# Eastern Time
eastern = pytz.timezone("US/Eastern")

# Connection status management
connection_status = {
    "1a": False,
    "1b": False,
    "last_1a_error": None,
    "last_1b_error": None
}

# Production data records
day_data = {
    "1a": {hour: {'value': 0, 'downtime': 0} for hour in range(24)},
    "1b": {hour: {'value': 0, 'downtime': 0} for hour in range(24)}
}

# Date data
current_date = {
    "day_month": 0,   # Current day's month
    "day_date": 0,    # Current day's date
    "night_month": 0, # Night shift month (current or previous day based on time)
    "night_date": 0   # Night shift date (current or previous day based on time)
}

# Target value
TARGET_VALUE = 20  # hourly target

# Cycle time in seconds for downtime calculation
CYCLE_TIME = 150  # seconds per unit

# Initialize database
def init_database():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Create production_data table if it doesn't exist
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS production_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            hour INTEGER,
            line_id TEXT,
            shift TEXT,
            target INTEGER,
            actual INTEGER,
            downtime INTEGER,
            created_at TEXT
        )
        ''')
        
        # Create daily_summary table for aggregated data
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            line_id TEXT,
            shift TEXT,
            total_target INTEGER,
            total_actual INTEGER,
            total_downtime INTEGER,
            created_at TEXT
        )
        ''')

        # Check if downtime column exists in production_data
        cursor.execute("PRAGMA table_info(production_data)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'downtime' not in columns:
            # Add downtime column if it doesn't exist
            cursor.execute("ALTER TABLE production_data ADD COLUMN downtime INTEGER DEFAULT 0")
            logger.info("Added downtime column to production_data table")
        
        # Check if total_downtime column exists in daily_summary
        cursor.execute("PRAGMA table_info(daily_summary)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'total_downtime' not in columns:
            # Add total_downtime column if it doesn't exist
            cursor.execute("ALTER TABLE daily_summary ADD COLUMN total_downtime INTEGER DEFAULT 0")
            logger.info("Added total_downtime column to daily_summary table")

        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")

# Calculate downtime in minutes from production units
def calculate_downtime_minutes(actual_units):
    # Formula: (3600 seconds per hour - (units * cycle time in seconds)) / 60 to get minutes
    # Ensure downtime is not negative
    downtime_minutes = max(0, int((3600 - (actual_units * CYCLE_TIME)) / 60))
    return downtime_minutes

# Save production data to database
def save_to_database(date_str, hour, line_id, shift, target, actual):
    try:
        # Calculate downtime in minutes
        downtime_minutes = calculate_downtime_minutes(actual)
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Check if record already exists
        cursor.execute(
            "SELECT id FROM production_data WHERE date=? AND hour=? AND line_id=?", 
            (date_str, hour, line_id)
        )
        record = cursor.fetchone()
        
        now = datetime.now(eastern).strftime("%Y-%m-%d %H:%M:%S")
        
        if record:
            # Update existing record
            cursor.execute(
                "UPDATE production_data SET target=?, actual=?, downtime=?, created_at=? WHERE date=? AND hour=? AND line_id=?",
                (target, actual, downtime_minutes, now, date_str, hour, line_id)
            )
        else:
            # Insert new record
            cursor.execute(
                "INSERT INTO production_data (date, hour, line_id, shift, target, actual, downtime, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (date_str, hour, line_id, shift, target, actual, downtime_minutes, now)
            )

        
        conn.commit()
        conn.close()
        logger.debug(f"Saved to database: {date_str}, {hour}, {line_id}, {shift}, {target}, {actual}, downtime={downtime_minutes}")
    except Exception as e:
        logger.error(f"Database save error: {e}")

# Save daily summary to database
def save_daily_summary(date_str, line_id, shift, total_target, total_actual):
    try:
        # Calculate total downtime
        total_downtime = sum(calculate_downtime_minutes(actual) for actual in 
                             [total_actual / 12] * 12)  # Spread total over 12 hours for estimation
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Check if record already exists
        cursor.execute(
            "SELECT id FROM daily_summary WHERE date=? AND line_id=? AND shift=?", 
            (date_str, line_id, shift)
        )
        record = cursor.fetchone()
        
        now = datetime.now(eastern).strftime("%Y-%m-%d %H:%M:%S")
        
        if record:
            # Update existing record
            cursor.execute(
                "UPDATE daily_summary SET total_target=?, total_actual=?, total_downtime=?, created_at=? WHERE date=? AND line_id=? AND shift=?",
                (total_target, total_actual, total_downtime, now, date_str, line_id, shift)
            )
        else:
            # Insert new record
            cursor.execute(
                "INSERT INTO daily_summary (date, line_id, shift, total_target, total_actual, total_downtime, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (date_str, line_id, shift, total_target, total_actual, total_downtime, now)
            )
        
        conn.commit()
        conn.close()
        logger.debug(f"Saved summary to database: {date_str}, {line_id}, {shift}, {total_target}, {total_actual}, total_downtime={total_downtime}")
    except Exception as e:
        logger.error(f"Database summary save error: {e}")

# Calculate actual total downtime from hourly records
def calculate_actual_total_downtime(date_str, line_id, shift):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Get all hourly records for the specified date, line, and shift
        cursor.execute(
            "SELECT downtime FROM production_data WHERE date=? AND line_id=? AND shift=?", 
            (date_str, line_id, shift)
        )
        
        downtime_records = cursor.fetchall()
        conn.close()
        
        # Sum up all downtime values
        total_downtime = sum(record[0] for record in downtime_records if record[0] is not None)
        
        return total_downtime
    except Exception as e:
        logger.error(f"Error calculating total downtime: {e}")
        return 0

# Format date to YYYY-MM-DD format
def format_date(month, day):
    # Get current year
    year = datetime.now(eastern).year
    
    # Handle year transition (December to January)
    now = datetime.now(eastern)
    if now.month == 1 and month == 12:
        year -= 1
    
    try:
        date_obj = datetime(year, month, day)
        return date_obj.strftime("%Y-%m-%d")
    except ValueError as e:
        logger.error(f"Date formatting error: {e}")
        return None

# Process and save hourly data to database
def process_hourly_data():
    now = datetime.now(eastern)
    current_hour = now.hour
    
    # Format date strings for database
    day_date_str = format_date(current_date["day_month"], current_date["day_date"])
    night_date_str = format_date(current_date["night_month"], current_date["night_date"])
    
    if not day_date_str or not night_date_str:
        logger.error("Could not format date strings for database")
        return
    
    # Process day shift data (7am-6pm)
    day_hours = list(range(7, 19))
    # Process night shift data (7pm-6am)
    night_hours = list(range(19, 24)) + list(range(0, 7))
    
    # Process SLAP1A day shift
    for hour in day_hours:
        if (current_hour >= 7 and hour <= current_hour) or current_hour >= 19 or current_hour < 7:
            save_to_database(
                day_date_str, 
                hour, 
                "1A", 
                "Day", 
                TARGET_VALUE, 
                day_data["1a"][hour]['value'] if hour in day_data["1a"] else 0
            )
    
    # Process SLAP1A night shift
    for hour in night_hours:
        if (hour >= 19 and current_hour >= hour) or (hour < 7 and current_hour >= hour) or current_hour >= 7:
            save_to_database(
                night_date_str, 
                hour, 
                "1A", 
                "Night", 
                TARGET_VALUE, 
                day_data["1a"][hour]['value'] if hour in day_data["1a"] else 0
            )
    
    # Process SLAP1B day shift
    for hour in day_hours:
        if (current_hour >= 7 and hour <= current_hour) or current_hour >= 19 or current_hour < 7:
            save_to_database(
                day_date_str, 
                hour, 
                "1B", 
                "Day", 
                TARGET_VALUE, 
                day_data["1b"][hour]['value'] if hour in day_data["1b"] else 0
            )
    
    # Process SLAP1B night shift
    for hour in night_hours:
        if (hour >= 19 and current_hour >= hour) or (hour < 7 and current_hour >= hour) or current_hour >= 7:
            save_to_database(
                night_date_str, 
                hour, 
                "1B", 
                "Night", 
                TARGET_VALUE, 
                day_data["1b"][hour]['value'] if hour in day_data["1b"] else 0
            )
    
    # Calculate and save daily summaries
    # SLAP1A Day Shift
    day_total_1a = sum(day_data["1a"][hour]['value'] if hour in day_data["1a"] else 0 for hour in day_hours if 
                      (current_hour >= 7 and hour <= current_hour) or current_hour >= 19 or current_hour < 7)
    # Always use full shift (12 hours) for target total
    day_target_total = TARGET_VALUE * 12
    save_daily_summary(day_date_str, "1A", "Day", day_target_total, day_total_1a)
    
    # SLAP1B Day Shift
    day_total_1b = sum(day_data["1b"][hour]['value'] if hour in day_data["1b"] else 0 for hour in day_hours if 
                      (current_hour >= 7 and hour <= current_hour) or current_hour >= 19 or current_hour < 7)
    save_daily_summary(day_date_str, "1B", "Day", day_target_total, day_total_1b)
    
    # SLAP1A Night Shift
    night_target_total = TARGET_VALUE * 12
    night_total_1a = sum(day_data["1a"][hour]['value'] if hour in day_data["1a"] else 0 for hour in night_hours if 
                        (hour >= 19 and current_hour >= hour) or 
                        (hour < 7 and current_hour >= hour) or 
                        current_hour >= 7)
    save_daily_summary(night_date_str, "1A", "Night", night_target_total, night_total_1a)

    # SLAP1B Night Shift
    night_total_1b = sum(day_data["1b"][hour]['value'] if hour in day_data["1b"] else 0 for hour in night_hours if 
                        (hour >= 19 and current_hour >= hour) or 
                        (hour < 7 and current_hour >= hour) or 
                        current_hour >= 7)
    save_daily_summary(night_date_str, "1B", "Night", night_target_total, night_total_1b)

# Read data from PLC (for SLAP1A)
def read_plc_1a():
    global connection_status
    plc = pymcprotocol.Type3E()
    
    # Connection attempt
    retry_count = 0
    connected = False
    
    while retry_count < MAX_CONNECTION_RETRIES and not connected:
        try:
            plc.connect(PLC_IP_1A, PLC_PORT)
            connected = True
            connection_status["1a"] = True
            
            # Log recovery if there was a previous error
            if connection_status["last_1a_error"]:
                logger.info(f"SLAP1A PLC connection recovered. IP: {PLC_IP_1A}")
                connection_status["last_1a_error"] = None
                
        except Exception as e:
            retry_count += 1
            error_msg = f"SLAP1A PLC connection error (attempt {retry_count}/{MAX_CONNECTION_RETRIES}): {str(e)}"
            logger.error(error_msg)
            connection_status["1a"] = False
            connection_status["last_1a_error"] = str(e)
            
            if retry_count < MAX_CONNECTION_RETRIES:
                logger.info(f"Will try to reconnect in {RETRY_INTERVAL} seconds...")
                eventlet.sleep(RETRY_INTERVAL)
            else:
                logger.error(f"Failed to connect to SLAP1A PLC. Maximum attempts reached.")
                return False
    
    try:
        if not connected:
            return False
            
        # Get current time (Eastern Time)
        now = datetime.now(eastern)
        current_hour = now.hour
        
        # Get date information directly
        day_month = plc.batchread_wordunits("D700", 1)[0]
        day_date = plc.batchread_wordunits("D710", 1)[0]
        
        yesterday_month = plc.batchread_wordunits("D701", 1)[0]
        yesterday_date = plc.batchread_wordunits("D711", 1)[0]  # Using D711 as specified
        
        # Log date values for debugging
        logger.debug(f"Date values from SLAP1A: day_month={day_month}, day_date={day_date}, yesterday_month={yesterday_month}, yesterday_date={yesterday_date}")
        
        # Update Day Shift date information (always use current day)
        current_date["day_month"] = day_month
        current_date["day_date"] = day_date
        
        # Night Shift date setting
        # Between 7pm-7am: use current day date (D700/D710)
        # Between 7am-7pm: use previous day date (D701/D711)
        if (current_hour >= 19) or (current_hour < 7):
            # Night shift in progress - use current date
            current_date["night_month"] = day_month
            current_date["night_date"] = day_date
            logger.debug("Night shift in progress, using current day date for night shift display")
        else:
            # Day shift in progress - use previous date for night shift display
            current_date["night_month"] = yesterday_month
            current_date["night_date"] = yesterday_date
            logger.debug("Day shift in progress, using previous day date for night shift display")
        
        # Get production data based on current time
        if current_hour < 7:
            # Between 0-7am:
            # - Night Shift (19-6): use current day's data (D819-D823, D800-D806)
            # - Day Shift (7-18): use current day's data (D807-D818)
            
            # Read current day's Night Shift data
            for i in range(19, 24):
                d_code = 800 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
                    
            for i in range(0, 7):
                d_code = 800 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
            
            # Read current day's Day Shift data
            for i in range(7, 19):
                d_code = 800 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
                    
        elif current_hour >= 7 and current_hour < 19:
            # Between 7am-7pm:
            # - Day Shift (7-current_hour): use current day's data (D807-D8xx)
            # - Night Shift (19-6): use previous day's data (D869-D873, D850-D856)
            
            # Read current day's Day Shift data up to current hour
            for i in range(7, min(current_hour + 1, 19)):
                d_code = 800 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
            
            # Read previous day's Night Shift data
            for i in range(19, 24):
                d_code = 850 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
                    
            for i in range(0, 7):
                d_code = 850 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
                    
        else:  # current_hour >= 19
            # Between 7pm-12am:
            # - Night Shift (19-current_hour): use current day's data (D819-D8xx)
            # - Day Shift (7-18): use current day's data (D807-D818)
            
            # Read current day's Night Shift data up to current hour
            for i in range(19, min(current_hour + 1, 24)):
                d_code = 800 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
            
            # Read current day's Day Shift data
            for i in range(7, 19):
                d_code = 800 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")    
               
        # Log successful read
        logger.debug(f"SLAP1A data read successful: {current_hour} hours")
        
        return True
    except Exception as e:
        logger.error(f"SLAP1A data read error: {str(e)}")
        connection_status["1a"] = False
        connection_status["last_1a_error"] = str(e)
        return False
    finally:
        try:
            plc.close()
        except:
            pass

# Read data from PLC (for SLAP1B)
def read_plc_1b():
    global connection_status
    plc = pymcprotocol.Type3E()
    
    # Connection attempt
    retry_count = 0
    connected = False
    
    while retry_count < MAX_CONNECTION_RETRIES and not connected:
        try:
            plc.connect(PLC_IP_1B, PLC_PORT)
            connected = True
            connection_status["1b"] = True
            
            # Log recovery if there was a previous error
            if connection_status["last_1b_error"]:
                logger.info(f"SLAP1B PLC connection recovered. IP: {PLC_IP_1B}")
                connection_status["last_1b_error"] = None
                
        except Exception as e:
            retry_count += 1
            error_msg = f"SLAP1B PLC connection error (attempt {retry_count}/{MAX_CONNECTION_RETRIES}): {str(e)}"
            logger.error(error_msg)
            connection_status["1b"] = False
            connection_status["last_1b_error"] = str(e)
            
            if retry_count < MAX_CONNECTION_RETRIES:
                logger.info(f"Will try to reconnect in {RETRY_INTERVAL} seconds...")
                eventlet.sleep(RETRY_INTERVAL)
            else:
                logger.error(f"Failed to connect to SLAP1B PLC. Maximum attempts reached.")
                return False
    
    try:
        if not connected:
            return False
            
        # Get current time (Eastern Time)
        now = datetime.now(eastern)
        current_hour = now.hour
        
        # Get production data based on current time
        if current_hour < 7:
            # Between 0-7am:
            # - Night Shift (19-6): use current day's data (D819-D823, D800-D806)
            # - Day Shift (7-18): use current day's data (D807-D818)
            
            # Read current day's Night Shift data
            for i in range(19, 24):
                d_code = 800 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
                    
            for i in range(0, 7):
                d_code = 800 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
            
            # Read current day's Day Shift data
            for i in range(7, 19):
                d_code = 800 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
                    
        elif current_hour >= 7 and current_hour < 19:
            # Between 7am-7pm:
            # - Day Shift (7-current_hour): use current day's data (D807-D8xx)
            # - Night Shift (19-6): use previous day's data (D869-D873, D850-D856)
            
            # Read current day's Day Shift data up to current hour
            for i in range(7, min(current_hour + 1, 19)):
                d_code = 800 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
            
            # Read previous day's Night Shift data
            for i in range(19, 24):
                d_code = 850 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
                    
            for i in range(0, 7):
                d_code = 850 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
                    
        else:  # current_hour >= 19
            # Between 7pm-12am:
            # - Night Shift (19-current_hour): use current day's data (D819-D8xx)
            # - Day Shift (7-18): use current day's data (D807-D818)
            
            # Read current day's Night Shift data up to current hour
            for i in range(19, min(current_hour + 1, 24)):
                d_code = 800 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
            
            # Read current day's Day Shift data
            for i in range(7, 19):
                d_code = 800 + i
                try:
                    value = plc.batchread_wordunits(f"D{d_code}", 1)[0]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                except Exception as e:
                    logger.error(f"Error reading D{d_code}: {e}")
        
        # Log successful read
        logger.debug(f"SLAP1B data read successful: {current_hour} hours")
        
        return True
    except Exception as e:
        logger.error(f"SLAP1B data read error: {str(e)}")
        connection_status["1b"] = False
        connection_status["last_1b_error"] = str(e)
        return False
    finally:
        try:
            plc.close()
        except:
            pass

def read_plc():
    logger.info("PLC data reading service started")
    
    # Initialize the database
    init_database()
    
    # Track the last hour for detecting hour change
    last_hour = datetime.now(eastern).hour
    
    while True:
        try:
            # Current hour
            now = datetime.now(eastern)
            current_hour = now.hour
            
            # Read data from PLCs
            success_1a = read_plc_1a()
            success_1b = read_plc_1b()
            
            # Process and save to database only when hour changes
            if current_hour != last_hour:
                logger.info(f"Hour changed from {last_hour} to {current_hour}, saving data to database")
                process_hourly_data()
                last_hour = current_hour
            
            # Send data, date information, and connection status
            socketio.emit("update", {
                "1a": day_data["1a"],
                "1b": day_data["1b"],
                "date": current_date,
                "connection": connection_status
            })
            
            # Log data update status
            if success_1a or success_1b:
                logger.debug("Data update completed")
            else:
                logger.warning("Cannot connect to both PLCs. Data may not be updated.")
            
            # Wait 5 seconds
            eventlet.sleep(5)
        except Exception as e:
            logger.error(f"Unexpected error during data update: {e}")
            eventlet.sleep(5)

# Routes using Blueprint
@slap_bp.route("/")
def index():
    logger.info(f"Index page accessed via {SUBPATH}")
    return render_template("index.html", subpath=SUBPATH, reports_url="/reports")

# Functions to retrieve data from database
def get_production_history(start_date, end_date):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute(
            """
            SELECT date, line_id, shift, 
                   SUM(target) as total_target, 
                   SUM(actual) as total_actual
            FROM production_data
            WHERE date BETWEEN ? AND ?
            GROUP BY date, line_id, shift
            ORDER BY date DESC, line_id, shift
            """,
            (start_date, end_date)
        )
        
        results = cursor.fetchall()
        
        # Convert to dictionary format
        history = []
        for row in results:
            date, line_id, shift, total_target, total_actual = row
            history.append({
                "date": date,
                "line_id": line_id,
                "shift": shift,
                "total_target": total_target,
                "total_actual": total_actual,
                "achievement_rate": round(total_actual / total_target * 100 if total_target > 0 else 0, 1)
            })
        
        conn.close()
        return history
    except Exception as e:
        logger.error(f"Database query error: {e}")
        return []

def get_hourly_production(date, line_id, shift):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute(
            """
            SELECT hour, target, actual
            FROM production_data
            WHERE date = ? AND line_id = ? AND shift = ?
            ORDER BY hour
            """,
            (date, line_id, shift)
        )
        
        results = cursor.fetchall()
        
        # Convert to dictionary format
        hourly_data = []
        for row in results:
            hour, target, actual = row
            hourly_data.append({
                "hour": hour,
                "target": target,
                "actual": actual,
                "achievement_rate": round(actual / target * 100 if target > 0 else 0, 1)
            })
        
        conn.close()
        return hourly_data
    except Exception as e:
        logger.error(f"Database hourly query error: {e}")
        return []

# Add a simple test route for debugging
@slap_bp.route("/test")
def test_route():
    return "Test route is working! Reports should be available."

# Register the blueprint with the Flask application
app.register_blueprint(slap_bp)

# Add a root route that redirects to the blueprint
@app.route("/")
def root():
    return f"<h1>SLAP Casting Output Service</h1><p>Access the application at <a href='{SUBPATH}'>{SUBPATH}</a></p>"

# Start PLC monitoring in background
eventlet.spawn(read_plc)

# Context processor to make subpath available in templates
@app.context_processor
def inject_subpath():
    return dict(subpath=SUBPATH)

# Server startup
if __name__ == "__main__":
    try:
        logger.info(f"Starting SLAP Casting Output service on {SUBPATH}...")
        socketio.run(app, host="0.0.0.0", port=5111, debug=True, use_reloader=False)
    except Exception as e:
        logger.critical(f"Server startup error: {e}")
    finally:
        logger.info("Shutting down service...")