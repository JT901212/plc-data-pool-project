import eventlet
eventlet.monkey_patch()  # ← Apply first
import time
import requests
import eventlet
from flask import Flask, render_template, Blueprint
from flask_socketio import SocketIO
from flask import jsonify, request
import logging
import os
import sqlite3
from datetime import datetime, timedelta
import pytz

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('slap_casting.log')
    ]
)

logger = logging.getLogger('slap_casting')

eastern = pytz.timezone('US/Eastern')

DB_FILE = 'slap_casting.db'

SUBPATH = os.getenv('SUBPATH', '/slap1cast')  # Default subpath

app = Flask(__name__)
app.config['SECRET_KEY'] = 'slap1-casting-secret-key'

socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*", path=f"{SUBPATH}/socket.io")

slap_bp = Blueprint('slap1', __name__, url_prefix=SUBPATH)

MASTER_API_BASE_URL = os.getenv('MASTER_API_BASE_URL', 'http://localhost:8000')

MAX_CONNECTION_RETRIES = 5
RETRY_INTERVAL = 3

day_data = {
    "1a": {i: {'value': 0, 'downtime': 0} for i in range(24)},
    "1b": {i: {'value': 0, 'downtime': 0} for i in range(24)}
}

current_date = {
    "day_month": 0,
    "day_date": 0,
    "night_month": 0,
    "night_date": 0
}

connection_status = {
    "1a": False,
    "1b": False,
    "last_1a_error": None,
    "last_1b_error": None
}

def init_database():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS production_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            line_id TEXT,
            shift TEXT,
            hour INTEGER,
            target INTEGER,
            actual INTEGER,
            downtime INTEGER,
            created_at TEXT
        )
        ''')
        
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
        
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        return False

def calculate_downtime_minutes(value):
    return value * 5  # Example: each unit represents 5 minutes

def save_to_database(date_str, hour, line_id, shift):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        data_key = "1a" if line_id == "SLAP1A" else "1b"
        
        hour_data = day_data[data_key][hour]
        value = hour_data['value']
        downtime = hour_data['downtime']
        
        target = 60  # Example: 60 units per hour target
        
        cursor.execute(
            "SELECT id FROM production_data WHERE date = ? AND line_id = ? AND shift = ? AND hour = ?",
            (date_str, line_id, shift, hour)
        )
        existing = cursor.fetchone()
        
        if existing:
            cursor.execute(
                """
                UPDATE production_data 
                SET actual = ?, downtime = ?, target = ?, created_at = ?
                WHERE date = ? AND line_id = ? AND shift = ? AND hour = ?
                """,
                (value, downtime, target, datetime.now().isoformat(), date_str, line_id, shift, hour)
            )
        else:
            cursor.execute(
                """
                INSERT INTO production_data (date, line_id, shift, hour, target, actual, downtime, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (date_str, line_id, shift, hour, target, value, downtime, datetime.now().isoformat())
            )
        
        conn.commit()
        conn.close()
        logger.debug(f"Saved hour {hour} data for {line_id} on {date_str} ({shift}): {value} units, {downtime} minutes downtime")
        return True
    except Exception as e:
        logger.error(f"Database save error: {e}")
        return False

def save_daily_summary(date_str, line_id, shift):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute(
            """
            SELECT SUM(target) as total_target, 
                   SUM(actual) as total_actual,
                   SUM(downtime) as total_downtime
            FROM production_data
            WHERE date = ? AND line_id = ? AND shift = ?
            """,
            (date_str, line_id, shift)
        )
        
        result = cursor.fetchone()
        
        if result and result[0] is not None:
            total_target, total_actual, total_downtime = result
            
            cursor.execute(
                "SELECT id FROM daily_summary WHERE date = ? AND line_id = ? AND shift = ?",
                (date_str, line_id, shift)
            )
            existing = cursor.fetchone()
            
            if existing:
                cursor.execute(
                    """
                    UPDATE daily_summary 
                    SET total_target = ?, total_actual = ?, total_downtime = ?, created_at = ?
                    WHERE date = ? AND line_id = ? AND shift = ?
                    """,
                    (total_target, total_actual, total_downtime, datetime.now().isoformat(), date_str, line_id, shift)
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO daily_summary (date, line_id, shift, total_target, total_actual, total_downtime, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (date_str, line_id, shift, total_target, total_actual, total_downtime, datetime.now().isoformat())
                )
            
            conn.commit()
            conn.close()
            logger.info(f"Saved daily summary for {line_id} on {date_str} ({shift}): {total_actual}/{total_target} units, {total_downtime} minutes downtime")
            return True
        else:
            logger.warning(f"No hourly data found for {line_id} on {date_str} ({shift})")
            conn.close()
            return False
    except Exception as e:
        logger.error(f"Daily summary save error: {e}")
        return False

def calculate_actual_total_downtime(shift, line_id):
    try:
        data_key = "1a" if line_id == "SLAP1A" else "1b"
        
        total_downtime = 0
        
        if shift == "day":
            for hour in range(7, 19):
                total_downtime += day_data[data_key][hour]['downtime']
        else:  # shift == "night"
            for hour in range(19, 24):
                total_downtime += day_data[data_key][hour]['downtime']
            for hour in range(0, 7):
                total_downtime += day_data[data_key][hour]['downtime']
        
        return total_downtime
    except Exception as e:
        logger.error(f"Downtime calculation error: {e}")
        return 0

def format_date(month, day):
    try:
        current_year = datetime.now(eastern).year
        
        date_obj = datetime(current_year, month, day)
        
        return date_obj.strftime("%Y-%m-%d")
    except Exception as e:
        logger.error(f"Date formatting error: {e}")
        return None

def make_api_request(endpoint, timeout=10):
    """Make HTTP request to master API with error handling"""
    try:
        url = f"{MASTER_API_BASE_URL}{endpoint}"
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed for {endpoint}: {e}")
        return None

def get_plc_registers(plc_id, register_list):
    """Get specific registers from PLC via master API"""
    if not register_list:
        return {}
    
    register_string = ",".join(register_list)
    endpoint = f"/api/plc/{plc_id}/registers?registers={register_string}"
    data = make_api_request(endpoint)
    
    if data and 'registers' in data:
        return data['registers']
    return {}

def process_hourly_data():
    now = datetime.now(eastern)
    current_hour = now.hour
    
    process_hour = (current_hour - 1) % 24
    
    if process_hour >= 7 and process_hour < 19:
        shift = "day"
        if current_date["day_month"] > 0 and current_date["day_date"] > 0:
            date_str = format_date(current_date["day_month"], current_date["day_date"])
        else:
            date_str = now.strftime("%Y-%m-%d")
    else:
        shift = "night"
        if current_date["night_month"] > 0 and current_date["night_date"] > 0:
            date_str = format_date(current_date["night_month"], current_date["night_date"])
        else:
            date_str = now.strftime("%Y-%m-%d")
    
    if not date_str:
        logger.error("Cannot determine date for hourly data processing")
        return False
    
    success_1a = save_to_database(date_str, process_hour, "SLAP1A", shift)
    success_1b = save_to_database(date_str, process_hour, "SLAP1B", shift)
    
    if process_hour == 18:  # End of day shift (6pm)
        save_daily_summary(date_str, "SLAP1A", "day")
        save_daily_summary(date_str, "SLAP1B", "day")
    elif process_hour == 6:  # End of night shift (6am)
        save_daily_summary(date_str, "SLAP1A", "night")
        save_daily_summary(date_str, "SLAP1B", "night")
    
    return success_1a or success_1b

def read_plc_1a():
    global connection_status
    
    try:
        now = datetime.now(eastern)
        current_hour = now.hour
        
        date_registers = get_plc_registers("1A", ["D700", "D710", "D701", "D711"])
        
        if not date_registers:
            connection_status["1a"] = False
            connection_status["last_1a_error"] = "Failed to get date information from API"
            return False
            
        day_month = date_registers.get("D700", 0)
        day_date = date_registers.get("D710", 0)
        yesterday_month = date_registers.get("D701", 0)
        yesterday_date = date_registers.get("D711", 0)
        
        logger.debug(f"Date values from SLAP1A: day_month={day_month}, day_date={day_date}, yesterday_month={yesterday_month}, yesterday_date={yesterday_date}")
        
        current_date["day_month"] = day_month
        current_date["day_date"] = day_date
        
        if (current_hour >= 19) or (current_hour < 7):
            current_date["night_month"] = day_month
            current_date["night_date"] = day_date
            logger.debug("Night shift in progress, using current day date for night shift display")
        else:
            current_date["night_month"] = yesterday_month
            current_date["night_date"] = yesterday_date
            logger.debug("Day shift in progress, using previous day date for night shift display")
        
        registers_to_read = []
        
        if current_hour < 7:
            registers_to_read.extend([f"D{800 + i}" for i in range(19, 24)])  # Night 19-23
            registers_to_read.extend([f"D{800 + i}" for i in range(0, 19)])   # Early morning + day
        elif current_hour >= 7 and current_hour < 19:
            registers_to_read.extend([f"D{800 + i}" for i in range(7, min(current_hour + 1, 19))])
            registers_to_read.extend([f"D{850 + i}" for i in range(19, 24)])  # Previous night 19-23
            registers_to_read.extend([f"D{850 + i}" for i in range(0, 7)])    # Previous night 0-6
        else:  # current_hour >= 19
            registers_to_read.extend([f"D{800 + i}" for i in range(19, min(current_hour + 1, 24))])
            registers_to_read.extend([f"D{800 + i}" for i in range(7, 19)])   # Day shift
        
        production_data = get_plc_registers("1A", registers_to_read)
        
        if not production_data:
            connection_status["1a"] = False
            connection_status["last_1a_error"] = "Failed to get production data from API"
            return False
        
        if current_hour < 7:
            for i in range(19, 24):
                d_code = f"D{800 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                    
            for i in range(0, 7):
                d_code = f"D{800 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
            
            for i in range(7, 19):
                d_code = f"D{800 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                    
        elif current_hour >= 7 and current_hour < 19:
            for i in range(7, min(current_hour + 1, 19)):
                d_code = f"D{800 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
            
            for i in range(19, 24):
                d_code = f"D{850 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                    
            for i in range(0, 7):
                d_code = f"D{850 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                    
        else:  # current_hour >= 19
            for i in range(19, min(current_hour + 1, 24)):
                d_code = f"D{800 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
            
            for i in range(7, 19):
                d_code = f"D{800 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1a"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
        
        logger.debug(f"SLAP1A data read successful: {current_hour} hours")
        connection_status["1a"] = True
        
        if connection_status["last_1a_error"]:
            logger.info(f"SLAP1A API connection recovered")
            connection_status["last_1a_error"] = None
        
        return True
        
    except Exception as e:
        logger.error(f"SLAP1A data read error: {str(e)}")
        connection_status["1a"] = False
        connection_status["last_1a_error"] = str(e)
        return False

def read_plc_1b():
    global connection_status
    
    try:
        now = datetime.now(eastern)
        current_hour = now.hour
        
        registers_to_read = []
        
        if current_hour < 7:
            registers_to_read.extend([f"D{800 + i}" for i in range(19, 24)])  # Night 19-23
            registers_to_read.extend([f"D{800 + i}" for i in range(0, 19)])   # Early morning + day
        elif current_hour >= 7 and current_hour < 19:
            registers_to_read.extend([f"D{800 + i}" for i in range(7, min(current_hour + 1, 19))])
            registers_to_read.extend([f"D{850 + i}" for i in range(19, 24)])  # Previous night 19-23
            registers_to_read.extend([f"D{850 + i}" for i in range(0, 7)])    # Previous night 0-6
        else:  # current_hour >= 19
            registers_to_read.extend([f"D{800 + i}" for i in range(19, min(current_hour + 1, 24))])
            registers_to_read.extend([f"D{800 + i}" for i in range(7, 19)])   # Day shift
        
        production_data = get_plc_registers("1B", registers_to_read)
        
        if not production_data:
            connection_status["1b"] = False
            connection_status["last_1b_error"] = "Failed to get production data from API"
            return False
        
        if current_hour < 7:
            for i in range(19, 24):
                d_code = f"D{800 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                    
            for i in range(0, 7):
                d_code = f"D{800 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
            
            for i in range(7, 19):
                d_code = f"D{800 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                    
        elif current_hour >= 7 and current_hour < 19:
            for i in range(7, min(current_hour + 1, 19)):
                d_code = f"D{800 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
            
            for i in range(19, 24):
                d_code = f"D{850 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                    
            for i in range(0, 7):
                d_code = f"D{850 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
                    
        else:  # current_hour >= 19
            for i in range(19, min(current_hour + 1, 24)):
                d_code = f"D{800 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
            
            for i in range(7, 19):
                d_code = f"D{800 + i}"
                if d_code in production_data:
                    value = production_data[d_code]
                    day_data["1b"][i] = {
                        'value': value,
                        'downtime': calculate_downtime_minutes(value)
                    }
        
        logger.debug(f"SLAP1B data read successful: {current_hour} hours")
        connection_status["1b"] = True
        
        if connection_status["last_1b_error"]:
            logger.info(f"SLAP1B API connection recovered")
            connection_status["last_1b_error"] = None
        
        return True
        
    except Exception as e:
        logger.error(f"SLAP1B data read error: {str(e)}")
        connection_status["1b"] = False
        connection_status["last_1b_error"] = str(e)
        return False

def read_plc():
    logger.info("PLC data reading service started")
    
    init_database()
    
    last_hour = datetime.now(eastern).hour
    
    while True:
        try:
            now = datetime.now(eastern)
            current_hour = now.hour
            
            success_1a = read_plc_1a()
            success_1b = read_plc_1b()
            
            if current_hour != last_hour:
                logger.info(f"Hour changed from {last_hour} to {current_hour}, saving data to database")
                process_hourly_data()
                last_hour = current_hour
            
            socketio.emit("update", {
                "1a": day_data["1a"],
                "1b": day_data["1b"],
                "date": current_date,
                "connection": connection_status
            })
            
            if success_1a or success_1b:
                logger.debug("Data update completed")
            else:
                logger.warning("Cannot connect to both PLCs. Data may not be updated.")
            
            eventlet.sleep(5)
        except Exception as e:
            logger.error(f"Unexpected error during data update: {e}")
            eventlet.sleep(5)

@slap_bp.route("/")
def index():
    logger.info(f"Index page accessed via {SUBPATH}")
    return render_template("index.html", subpath=SUBPATH, reports_url="/reports")

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

@slap_bp.route("/test")
def test_route():
    return "Test route is working! Reports should be available."

app.register_blueprint(slap_bp)

@app.route("/")
def root():
    return f"<h1>SLAP Casting Output Service</h1><p>Access the application at <a href='{SUBPATH}'>{SUBPATH}</a></p>"

eventlet.spawn(read_plc)

@app.context_processor
def inject_subpath():
    return dict(subpath=SUBPATH)

if __name__ == "__main__":
    try:
        logger.info(f"Starting SLAP Casting Output service on {SUBPATH}...")
        socketio.run(app, host="0.0.0.0", port=5111, debug=True, use_reloader=False, log_output=True)
    except Exception as e:
        logger.critical(f"Server startup error: {e}")
    finally:
        logger.info("Shutting down service...")
