#!/usr/bin/env python3
"""
PLC Data Pool Master
Centralized PLC data collection and distribution service
"""

import json
import sqlite3
import time
import threading
import logging
import os
import signal
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from contextlib import contextmanager

# Flask imports
from flask import Flask, jsonify, request
from flask_cors import CORS

# PLC library
try:
    import pymcprotocol
    PLC_AVAILABLE = True
except ImportError:
    print("Warning: pymcprotocol not installed. Install with: pip install pymcprotocol")
    PLC_AVAILABLE = False

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('plc_master.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('PLCMaster')

@dataclass
class PLCRegisterRange:
    """Define PLC register reading range"""
    start_register: str
    count: int
    description: str

@dataclass
class PLCConfig:
    """PLC configuration"""
    plc_id: str
    name: str
    ip: str
    port: int = 5020
    enabled: bool = True
    register_ranges: List[PLCRegisterRange] = None

@dataclass
class PLCData:
    """PLC data with metadata"""
    plc_id: str
    timestamp: datetime
    registers: Dict[str, int]
    connection_status: bool
    error_message: Optional[str] = None

class PLCDataPool:
    """Centralized PLC data management"""
    
    def __init__(self, config_file: str = "plc_config.json", db_file: str = "plc_master.db"):
        self.config_file = config_file
        self.db_file = db_file
        self.data_file = "plc_current_data.json"
        
        # Internal data storage
        self.current_data: Dict[str, PLCData] = {}
        self.plc_configs: Dict[str, PLCConfig] = {}
        self.plc_connections: Dict[str, pymcprotocol.Type3E] = {}
        self.connection_status: Dict[str, bool] = {}
        
        # Threading
        self.data_lock = threading.RLock()
        self.running = False
        self.threads = []
        
        # Load configuration
        self.load_config()
        self.init_database()
        
        logger.info("PLCDataPool initialized")

    def load_config(self):
        """Load PLC configuration from JSON file"""
        default_config = {
            "plcs": {
                "1A": {
                    "name": "SLAP1A",
                    "ip": "192.168.150.22",
                    "port": 5020,
                    "enabled": True,
                    "register_ranges": [
                        {"start_register": "D52", "count": 1, "description": "Date"},
                        {"start_register": "D700", "count": 20, "description": "Date info"},
                        {"start_register": "D800", "count": 100, "description": "Production current day"},
                        {"start_register": "D850", "count": 100, "description": "Production previous day"},
                        {"start_register": "D5000", "count": 100, "description": "Alarm data today"},
                        {"start_register": "D5100", "count": 100, "description": "Alarm data yesterday"},
                        {"start_register": "D10100", "count": 100, "description": "Time slot 7-11am"},
                        {"start_register": "D10200", "count": 100, "description": "Time slot 11am-3pm"},
                        {"start_register": "D10300", "count": 100, "description": "Time slot 3-7pm"},
                        {"start_register": "D10400", "count": 100, "description": "Time slot 7-11pm"},
                        {"start_register": "D10500", "count": 100, "description": "Time slot 11pm-3am"},
                        {"start_register": "D10600", "count": 100, "description": "Time slot 3-7am"}
                    ]
                },
                "1B": {
                    "name": "SLAP1B", 
                    "ip": "192.168.150.24",
                    "port": 5020,
                    "enabled": True,
                    "register_ranges": [
                        {"start_register": "D52", "count": 1, "description": "Date"},
                        {"start_register": "D700", "count": 20, "description": "Date info"},
                        {"start_register": "D800", "count": 100, "description": "Production current day"},
                        {"start_register": "D850", "count": 100, "description": "Production previous day"},
                        {"start_register": "D5000", "count": 100, "description": "Alarm data today"},
                        {"start_register": "D5100", "count": 100, "description": "Alarm data yesterday"},
                        {"start_register": "D10100", "count": 100, "description": "Time slot 7-11am"},
                        {"start_register": "D10200", "count": 100, "description": "Time slot 11am-3pm"},
                        {"start_register": "D10300", "count": 100, "description": "Time slot 3-7pm"},
                        {"start_register": "D10400", "count": 100, "description": "Time slot 7-11pm"},
                        {"start_register": "D10500", "count": 100, "description": "Time slot 11pm-3am"},
                        {"start_register": "D10600", "count": 100, "description": "Time slot 3-7am"}
                    ]
                }
            },
            "settings": {
                "poll_interval": 3,
                "connection_timeout": 10,
                "max_retries": 3,
                "retry_delay": 5,
                "history_retention_hours": 24
            }
        }
        
        if not os.path.exists(self.config_file):
            logger.info(f"Creating default config file: {self.config_file}")
            with open(self.config_file, 'w') as f:
                json.dump(default_config, f, indent=2)
        
        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)
            
            self.settings = config.get('settings', default_config['settings'])
            
            # Parse PLC configurations
            for plc_id, plc_data in config.get('plcs', {}).items():
                register_ranges = []
                for range_data in plc_data.get('register_ranges', []):
                    register_ranges.append(PLCRegisterRange(**range_data))
                
                self.plc_configs[plc_id] = PLCConfig(
                    plc_id=plc_id,
                    name=plc_data.get('name', f'PLC_{plc_id}'),
                    ip=plc_data.get('ip'),
                    port=plc_data.get('port', 5020),
                    enabled=plc_data.get('enabled', True),
                    register_ranges=register_ranges
                )
            
            logger.info(f"Loaded configuration for {len(self.plc_configs)} PLCs")
            
        except Exception as e:
            logger.error(f"Error loading configuration: {e}")
            raise

    def init_database(self):
        """Initialize SQLite database for historical data"""
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                
                # Create table for historical PLC data
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS plc_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        plc_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        register_name TEXT NOT NULL,
                        register_value INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Create index for better query performance
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_plc_timestamp 
                    ON plc_history (plc_id, timestamp)
                ''')
                
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_register_name 
                    ON plc_history (register_name)
                ''')
                
                # Create connection status table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS connection_status (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        plc_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        connected BOOLEAN NOT NULL,
                        error_message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                conn.commit()
                logger.info("Database initialized successfully")
                
        except Exception as e:
            logger.error(f"Error initializing database: {e}")
            raise

    def connect_plc(self, plc_id: str) -> bool:
        """Connect to specific PLC"""
        if not PLC_AVAILABLE:
            logger.error("pymcprotocol not available")
            return False
            
        config = self.plc_configs.get(plc_id)
        if not config or not config.enabled:
            return False
        
        try:
            if plc_id in self.plc_connections:
                try:
                    self.plc_connections[plc_id].close()
                except:
                    pass
            
            plc = pymcprotocol.Type3E()
            plc.connect(config.ip, config.port)
            
            # Set timeout if supported
            if hasattr(plc, '_sock') and plc._sock:
                plc._sock.settimeout(self.settings['connection_timeout'])
            
            self.plc_connections[plc_id] = plc
            self.connection_status[plc_id] = True
            
            logger.info(f"Connected to PLC {plc_id} ({config.name}) at {config.ip}:{config.port}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect to PLC {plc_id}: {e}")
            self.connection_status[plc_id] = False
            return False

    def read_plc_data(self, plc_id: str) -> Optional[PLCData]:
        """Read data from specific PLC"""
        config = self.plc_configs.get(plc_id)
        plc = self.plc_connections.get(plc_id)
        
        if not config or not plc:
            return None
        
        try:
            registers = {}
            
            # Read all register ranges for this PLC
            for range_config in config.register_ranges:
                try:
                    # Extract register number from register name (e.g., "D800" -> 800)
                    reg_num = int(range_config.start_register[1:])
                    
                    # Read data from PLC
                    values = plc.batchread_wordunits(range_config.start_register, range_config.count)
                    
                    # Store values with register names
                    for i, value in enumerate(values):
                        register_name = f"D{reg_num + i}"
                        registers[register_name] = value
                    
                    logger.debug(f"Read {range_config.count} registers from {range_config.start_register} ({range_config.description})")
                    
                except Exception as e:
                    logger.error(f"Error reading {range_config.start_register} from PLC {plc_id}: {e}")
                    # Continue reading other ranges even if one fails
                    continue
            
            if registers:
                plc_data = PLCData(
                    plc_id=plc_id,
                    timestamp=datetime.now(),
                    registers=registers,
                    connection_status=True
                )
                
                logger.debug(f"Successfully read {len(registers)} registers from PLC {plc_id}")
                return plc_data
            else:
                logger.warning(f"No data read from PLC {plc_id}")
                return None
                
        except Exception as e:
            logger.error(f"Error reading PLC {plc_id}: {e}")
            self.connection_status[plc_id] = False
            
            # Return error data
            return PLCData(
                plc_id=plc_id,
                timestamp=datetime.now(),
                registers={},
                connection_status=False,
                error_message=str(e)
            )

    def save_to_database(self, plc_data: PLCData):
        """Save PLC data to database"""
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                
                # Save register data
                timestamp_str = plc_data.timestamp.isoformat()
                for register_name, value in plc_data.registers.items():
                    cursor.execute('''
                        INSERT INTO plc_history (plc_id, timestamp, register_name, register_value)
                        VALUES (?, ?, ?, ?)
                    ''', (plc_data.plc_id, timestamp_str, register_name, value))
                
                # Save connection status
                cursor.execute('''
                    INSERT INTO connection_status (plc_id, timestamp, connected, error_message)
                    VALUES (?, ?, ?, ?)
                ''', (plc_data.plc_id, timestamp_str, plc_data.connection_status, plc_data.error_message))
                
                conn.commit()
                
        except Exception as e:
            logger.error(f"Error saving to database: {e}")

    def cleanup_old_data(self):
        """Remove old data beyond retention period"""
        try:
            cutoff_time = datetime.now() - timedelta(hours=self.settings['history_retention_hours'])
            cutoff_str = cutoff_time.isoformat()
            
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                
                # Clean up old history
                cursor.execute('DELETE FROM plc_history WHERE timestamp < ?', (cutoff_str,))
                history_deleted = cursor.rowcount
                
                cursor.execute('DELETE FROM connection_status WHERE timestamp < ?', (cutoff_str,))
                status_deleted = cursor.rowcount
                
                conn.commit()
                
                if history_deleted > 0 or status_deleted > 0:
                    logger.info(f"Cleaned up {history_deleted} history records and {status_deleted} status records")
                    
        except Exception as e:
            logger.error(f"Error cleaning up old data: {e}")

    def save_current_data(self):
        """Save current data to JSON file"""
        try:
            with self.data_lock:
                data_to_save = {}
                for plc_id, plc_data in self.current_data.items():
                    data_to_save[plc_id] = {
                        'plc_id': plc_data.plc_id,
                        'timestamp': plc_data.timestamp.isoformat(),
                        'registers': plc_data.registers,
                        'connection_status': plc_data.connection_status,
                        'error_message': plc_data.error_message
                    }
                
                with open(self.data_file, 'w') as f:
                    json.dump(data_to_save, f, indent=2)
                    
        except Exception as e:
            logger.error(f"Error saving current data: {e}")

    def data_collection_loop(self):
        """Main data collection loop"""
        logger.info("Starting data collection loop")
        
        retry_counts = {plc_id: 0 for plc_id in self.plc_configs.keys()}
        last_cleanup = time.time()
        
        while self.running:
            try:
                current_time = time.time()
                
                # Periodic cleanup (every hour)
                if current_time - last_cleanup > 3600:
                    self.cleanup_old_data()
                    last_cleanup = current_time
                
                # Process each enabled PLC
                for plc_id, config in self.plc_configs.items():
                    if not config.enabled:
                        continue
                    
                    # Check connection
                    if plc_id not in self.plc_connections or not self.connection_status.get(plc_id, False):
                        if retry_counts[plc_id] < self.settings['max_retries']:
                            logger.info(f"Attempting to connect to PLC {plc_id}")
                            if self.connect_plc(plc_id):
                                retry_counts[plc_id] = 0
                            else:
                                retry_counts[plc_id] += 1
                                time.sleep(self.settings['retry_delay'])
                                continue
                        else:
                            # Max retries reached, skip this cycle
                            continue
                    
                    # Read data
                    plc_data = self.read_plc_data(plc_id)
                    if plc_data:
                        with self.data_lock:
                            self.current_data[plc_id] = plc_data
                        
                        # Save to database
                        self.save_to_database(plc_data)
                        
                        retry_counts[plc_id] = 0
                    else:
                        retry_counts[plc_id] += 1
                
                # Save current data to file
                self.save_current_data()
                
                # Wait for next poll
                time.sleep(self.settings['poll_interval'])
                
            except Exception as e:
                logger.error(f"Error in data collection loop: {e}")
                time.sleep(5)

    def start(self):
        """Start the data collection service"""
        if self.running:
            logger.warning("Service already running")
            return
        
        self.running = True
        
        # Start data collection thread
        collection_thread = threading.Thread(target=self.data_collection_loop, daemon=True)
        collection_thread.start()
        self.threads.append(collection_thread)
        
        logger.info("PLCDataPool service started")

    def stop(self):
        """Stop the data collection service"""
        logger.info("Stopping PLCDataPool service")
        self.running = False
        
        # Close PLC connections
        for plc_id, plc in self.plc_connections.items():
            try:
                plc.close()
                logger.info(f"Closed connection to PLC {plc_id}")
            except:
                pass
        
        # Wait for threads
        for thread in self.threads:
            thread.join(timeout=5)
        
        logger.info("PLCDataPool service stopped")

    def get_current_data(self, plc_id: str = None) -> Dict[str, Any]:
        """Get current data for specific PLC or all PLCs"""
        with self.data_lock:
            if plc_id:
                plc_data = self.current_data.get(plc_id)
                if plc_data:
                    return {
                        'plc_id': plc_data.plc_id,
                        'timestamp': plc_data.timestamp.isoformat(),
                        'registers': plc_data.registers,
                        'connection_status': plc_data.connection_status,
                        'error_message': plc_data.error_message
                    }
                return {}
            else:
                result = {}
                for plc_id, plc_data in self.current_data.items():
                    result[plc_id] = {
                        'plc_id': plc_data.plc_id,
                        'timestamp': plc_data.timestamp.isoformat(),
                        'registers': plc_data.registers,
                        'connection_status': plc_data.connection_status,
                        'error_message': plc_data.error_message
                    }
                return result

    def get_register_data(self, plc_id: str, register_names: List[str]) -> Dict[str, int]:
        """Get specific register values"""
        with self.data_lock:
            plc_data = self.current_data.get(plc_id)
            if not plc_data:
                return {}
            
            result = {}
            for register_name in register_names:
                if register_name in plc_data.registers:
                    result[register_name] = plc_data.registers[register_name]
            
            return result

# Global data pool instance
data_pool = None

# Flask API setup
app = Flask(__name__)
CORS(app)

@app.route('/')
def index():
    """Serve the monitoring dashboard"""
    return '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PLC Data Pool Monitor</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
        }

        .header {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 20px 30px;
            margin-bottom: 20px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
        }

        .header h1 {
            color: #333;
            font-size: 2em;
            margin-bottom: 10px;
        }

        .status-bar {
            display: flex;
            gap: 20px;
            align-items: center;
        }

        .status-item {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 15px;
            border-radius: 20px;
            background: rgba(0, 0, 0, 0.05);
        }

        .status-indicator {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }

        .status-connected {
            background: #4CAF50;
        }

        .status-disconnected {
            background: #f44336;
        }

        .status-warning {
            background: #FF9800;
        }

        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }

        .plc-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(600px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }

        .plc-card {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 20px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            transition: transform 0.3s ease;
        }

        .plc-card:hover {
            transform: translateY(-5px);
        }

        .plc-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #eee;
        }

        .plc-title {
            font-size: 1.5em;
            font-weight: bold;
            color: #333;
        }

        .plc-status {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 5px 12px;
            border-radius: 15px;
            font-size: 0.9em;
            font-weight: bold;
        }

        .plc-connected {
            background: #e8f5e8;
            color: #2e7d32;
        }

        .plc-disconnected {
            background: #ffebee;
            color: #c62828;
        }

        .register-container {
            max-height: 400px;
            overflow-y: auto;
            border: 1px solid #ddd;
            border-radius: 8px;
        }

        .register-table {
            width: 100%;
            border-collapse: collapse;
        }

        .register-table th {
            background: #f5f5f5;
            padding: 12px 15px;
            text-align: left;
            font-weight: bold;
            border-bottom: 2px solid #ddd;
            position: sticky;
            top: 0;
        }

        .register-table td {
            padding: 8px 15px;
            border-bottom: 1px solid #eee;
            transition: background-color 0.3s ease;
        }

        .register-row:hover {
            background: #f8f9fa;
        }

        .register-name {
            font-family: 'Courier New', monospace;
            font-weight: bold;
            color: #2196F3;
        }

        .register-value {
            font-family: 'Courier New', monospace;
            text-align: right;
            font-weight: bold;
        }

        .register-value.changed {
            background: #fff3cd;
            color: #856404;
            animation: highlight 1s ease-out;
        }

        @keyframes highlight {
            0% { background: #28a745; color: white; }
            100% { background: #fff3cd; color: #856404; }
        }

        .controls {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
        }

        .control-row {
            display: flex;
            gap: 15px;
            align-items: center;
            margin-bottom: 10px;
        }

        .control-btn {
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-weight: bold;
            transition: all 0.3s ease;
        }

        .btn-primary {
            background: #2196F3;
            color: white;
        }

        .btn-primary:hover {
            background: #1976D2;
            transform: translateY(-2px);
        }

        .btn-success {
            background: #4CAF50;
            color: white;
        }

        .btn-success:hover {
            background: #45a049;
            transform: translateY(-2px);
        }

        .btn-danger {
            background: #f44336;
            color: white;
        }

        .btn-danger:hover {
            background: #da190b;
            transform: translateY(-2px);
        }

        .refresh-interval {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .refresh-interval select {
            padding: 8px 12px;
            border: 1px solid #ddd;
            border-radius: 5px;
        }

        .loading {
            text-align: center;
            padding: 40px;
            color: #666;
            font-size: 1.2em;
        }

        .error-message {
            background: #ffebee;
            color: #c62828;
            padding: 15px;
            border-radius: 8px;
            margin: 10px 0;
            border-left: 4px solid #f44336;
        }

        .last-updated {
            text-align: center;
            color: #666;
            font-size: 0.9em;
            margin-top: 10px;
        }

        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }

        .stat-card {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 10px;
            padding: 15px;
            text-align: center;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.1);
        }

        .stat-number {
            font-size: 2em;
            font-weight: bold;
            color: #2196F3;
        }

        .stat-label {
            color: #666;
            font-size: 0.9em;
            margin-top: 5px;
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div class="header">
            <h1>🏭 PLC Data Pool Monitor</h1>
            <div class="status-bar">
                <div class="status-item">
                    <div class="status-indicator" id="systemStatus"></div>
                    <span id="systemStatusText">System Status</span>
                </div>
                <div class="status-item">
                    <span>Last Update: <span id="lastUpdate">-</span></span>
                </div>
                <div class="status-item">
                    <span>Auto Refresh: <span id="autoRefreshStatus">ON</span></span>
                </div>
            </div>
        </div>

        <!-- Statistics -->
        <div class="stats">
            <div class="stat-card">
                <div class="stat-number" id="totalPlcs">0</div>
                <div class="stat-label">Total PLCs</div>
            </div>
            <div class="stat-card">
                <div class="stat-number" id="connectedPlcs">0</div>
                <div class="stat-label">Connected</div>
            </div>
            <div class="stat-card">
                <div class="stat-number" id="totalRegisters">0</div>
                <div class="stat-label">Total Registers</div>
            </div>
            <div class="stat-card">
                <div class="stat-number" id="dataRate">0</div>
                <div class="stat-label">Updates/min</div>
            </div>
        </div>

        <!-- Controls -->
        <div class="controls">
            <div class="control-row">
                <button class="control-btn btn-primary" onclick="refreshData()">🔄 Refresh Now</button>
                <button class="control-btn btn-success" id="autoRefreshBtn" onclick="toggleAutoRefresh()">⏸️ Pause Auto Refresh</button>
                <button class="control-btn btn-danger" onclick="clearData()">🗑️ Clear Display</button>
                
                <div class="refresh-interval">
                    <label>Refresh Interval:</label>
                    <select id="refreshInterval" onchange="setRefreshInterval()">
                        <option value="1000">1 second</option>
                        <option value="2000" selected>2 seconds</option>
                        <option value="5000">5 seconds</option>
                        <option value="10000">10 seconds</option>
                        <option value="30000">30 seconds</option>
                    </select>
                </div>
            </div>
        </div>

        <!-- Error Message -->
        <div id="errorContainer" style="display: none;">
            <div class="error-message" id="errorMessage"></div>
        </div>

        <!-- PLC Data Grid -->
        <div class="plc-grid" id="plcGrid">
            <!-- PLC cards will be populated here -->
        </div>

        <!-- Last Updated -->
        <div class="last-updated" id="lastUpdatedFooter">
            Data will refresh automatically every 2 seconds
        </div>
    </div>

    <script>
        // Global variables
        let autoRefresh = true;
        let refreshInterval = 2000;
        let refreshTimer = null;
        let previousData = {};
        let updateCount = 0;
        let startTime = Date.now();

        // API base URL - Use current host
        const API_BASE = window.location.origin + '/api';

        // Initialize the application
        document.addEventListener('DOMContentLoaded', function() {
            initializeApp();
        });

        function initializeApp() {
            console.log('Initializing PLC Data Pool Monitor...');
            refreshData();
            startAutoRefresh();
        }

        function refreshData() {
            hideError();
            
            // Fetch health status and all PLC data
            Promise.all([
                fetch(`${API_BASE}/health`).then(r => r.json()),
                fetch(`${API_BASE}/plc/all`).then(r => r.json())
            ])
            .then(([healthData, plcData]) => {
                updateSystemStatus(healthData);
                updatePlcData(plcData);
                updateStatistics(healthData, plcData);
                updateCount++;
                updateLastUpdated();
            })
            .catch(error => {
                console.error('Error fetching data:', error);
                showError(`Failed to fetch data: ${error.message}`);
            });
        }

        function updateSystemStatus(healthData) {
            const statusIndicator = document.getElementById('systemStatus');
            const statusText = document.getElementById('systemStatusText');
            
            if (healthData.status === 'ok') {
                statusIndicator.className = 'status-indicator status-connected';
                statusText.textContent = 'System Online';
            } else {
                statusIndicator.className = 'status-indicator status-disconnected';
                statusText.textContent = 'System Error';
            }
        }

        function updatePlcData(plcData) {
            const plcGrid = document.getElementById('plcGrid');
            plcGrid.innerHTML = '';

            Object.keys(plcData).forEach(plcId => {
                const plc = plcData[plcId];
                const plcCard = createPlcCard(plcId, plc);
                plcGrid.appendChild(plcCard);
            });

            // Store for change detection
            previousData = JSON.parse(JSON.stringify(plcData));
        }

        function createPlcCard(plcId, plcData) {
            const card = document.createElement('div');
            card.className = 'plc-card';
            card.id = `plc-${plcId}`;

            const statusClass = plcData.connection_status ? 'plc-connected' : 'plc-disconnected';
            const statusText = plcData.connection_status ? '🟢 Connected' : '🔴 Disconnected';
            
            let registersHtml = '';
            if (plcData.registers && Object.keys(plcData.registers).length > 0) {
                Object.keys(plcData.registers).sort().forEach(registerName => {
                    const value = plcData.registers[registerName];
                    const isChanged = previousData[plcId] && 
                                    previousData[plcId].registers && 
                                    previousData[plcId].registers[registerName] !== value;
                    
                    registersHtml += `
                        <tr class="register-row">
                            <td class="register-name">${registerName}</td>
                            <td class="register-value ${isChanged ? 'changed' : ''}">${value}</td>
                        </tr>
                    `;
                });
            } else {
                registersHtml = '<tr><td colspan="2" style="text-align: center; color: #999;">No data available</td></tr>';
            }

            card.innerHTML = `
                <div class="plc-header">
                    <div class="plc-title">PLC ${plcId}</div>
                    <div class="plc-status ${statusClass}">${statusText}</div>
                </div>
                <div class="register-container">
                    <table class="register-table">
                        <thead>
                            <tr>
                                <th>Register</th>
                                <th>Value</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${registersHtml}
                        </tbody>
                    </table>
                </div>
                <div style="margin-top: 10px; font-size: 0.9em; color: #666;">
                    Last Update: ${plcData.timestamp ? new Date(plcData.timestamp).toLocaleTimeString() : 'Never'}
                    ${plcData.error_message ? `<br>Error: ${plcData.error_message}` : ''}
                </div>
            `;

            return card;
        }

        function updateStatistics(healthData, plcData) {
            const totalPlcs = Object.keys(plcData).length;
            const connectedPlcs = Object.values(plcData).filter(p => p.connection_status).length;
            const totalRegisters = Object.values(plcData).reduce((sum, p) => 
                sum + (p.registers ? Object.keys(p.registers).length : 0), 0);
            const dataRate = Math.round((updateCount / (Date.now() - startTime)) * 60000);

            document.getElementById('totalPlcs').textContent = totalPlcs;
            document.getElementById('connectedPlcs').textContent = connectedPlcs;
            document.getElementById('totalRegisters').textContent = totalRegisters;
            document.getElementById('dataRate').textContent = dataRate;
        }

        function toggleAutoRefresh() {
            autoRefresh = !autoRefresh;
            const btn = document.getElementById('autoRefreshBtn');
            const status = document.getElementById('autoRefreshStatus');
            
            if (autoRefresh) {
                btn.innerHTML = '⏸️ Pause Auto Refresh';
                btn.className = 'control-btn btn-success';
                status.textContent = 'ON';
                startAutoRefresh();
            } else {
                btn.innerHTML = '▶️ Start Auto Refresh';
                btn.className = 'control-btn btn-primary';
                status.textContent = 'OFF';
                stopAutoRefresh();
            }
        }

        function startAutoRefresh() {
            if (refreshTimer) clearInterval(refreshTimer);
            if (autoRefresh) {
                refreshTimer = setInterval(refreshData, refreshInterval);
            }
        }

        function stopAutoRefresh() {
            if (refreshTimer) {
                clearInterval(refreshTimer);
                refreshTimer = null;
            }
        }

        function setRefreshInterval() {
            refreshInterval = parseInt(document.getElementById('refreshInterval').value);
            if (autoRefresh) {
                startAutoRefresh();
            }
        }

        function clearData() {
            document.getElementById('plcGrid').innerHTML = '<div class="loading">Data cleared. Click refresh to reload.</div>';
            previousData = {};
            updateCount = 0;
            startTime = Date.now();
        }

        function showLoading(show) {
            document.getElementById('loadingIndicator').style.display = show ? 'block' : 'none';
        }

        function showError(message) {
            document.getElementById('errorMessage').textContent = message;
            document.getElementById('errorContainer').style.display = 'block';
        }

        function hideError() {
            document.getElementById('errorContainer').style.display = 'none';
        }

        function updateLastUpdated() {
            const now = new Date().toLocaleTimeString();
            document.getElementById('lastUpdate').textContent = now;
            document.getElementById('lastUpdatedFooter').textContent = 
                `Last updated: ${now} | Next refresh in ${refreshInterval/1000} seconds`;
        }

        // Handle page visibility changes to pause/resume when tab is hidden
        document.addEventListener('visibilitychange', function() {
            if (document.hidden) {
                stopAutoRefresh();
            } else if (autoRefresh) {
                startAutoRefresh();
                refreshData(); // Refresh immediately when coming back
            }
        });
    </script>
</body>
</html>'''

@app.route('/api/health')
def health_check():
    """Health check endpoint"""
    if not data_pool:
        return jsonify({'status': 'error', 'message': 'Data pool not initialized'}), 500
    
    status = {
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'plcs': {}
    }
    
    for plc_id in data_pool.plc_configs.keys():
        plc_data = data_pool.current_data.get(plc_id)
        status['plcs'][plc_id] = {
            'connected': data_pool.connection_status.get(plc_id, False),
            'last_update': plc_data.timestamp.isoformat() if plc_data else None,
            'register_count': len(plc_data.registers) if plc_data else 0
        }
    
    return jsonify(status)

@app.route('/api/plc/<plc_id>/current')
def get_plc_current_data(plc_id):
    """Get current data for specific PLC"""
    if not data_pool:
        return jsonify({'error': 'Data pool not initialized'}), 500
    
    data = data_pool.get_current_data(plc_id)
    if not data:
        return jsonify({'error': f'No data available for PLC {plc_id}'}), 404
    
    return jsonify(data)

@app.route('/api/plc/<plc_id>/registers')
def get_plc_registers(plc_id):
    """Get specific registers for PLC"""
    if not data_pool:
        return jsonify({'error': 'Data pool not initialized'}), 500
    
    # Get register names from query parameters
    logger.debug(f"Raw query args for PLC {plc_id}: {request.args}")
    register_names = request.args.getlist('registers')
    logger.debug(f"Initial register_names: {register_names}")
    
    # Handle comma-separated values in a single parameter
    if len(register_names) == 1 and ',' in register_names[0]:
        register_names = register_names[0].split(',')
        logger.debug(f"Split register_names: {register_names}")
    
    # Remove any empty strings from the list
    register_names = [name.strip() for name in register_names if name.strip()]
    logger.debug(f"Final register_names: {register_names}")
    
    if not register_names:
        # If no specific registers requested, return all
        data = data_pool.get_current_data(plc_id)
        return jsonify(data.get('registers', {}))
    
    data = data_pool.get_register_data(plc_id, register_names)
    logger.debug(f"Register data result: {data}")
    return jsonify(data)

@app.route('/api/plc/all')
def get_all_plc_data():
    """Get current data for all PLCs"""
    if not data_pool:
        return jsonify({'error': 'Data pool not initialized'}), 500
    
    data = data_pool.get_current_data()
    return jsonify(data)

@app.route('/api/config')
def get_config():
    """Get PLC configuration"""
    if not data_pool:
        return jsonify({'error': 'Data pool not initialized'}), 500
    
    config = {}
    for plc_id, plc_config in data_pool.plc_configs.items():
        config[plc_id] = {
            'name': plc_config.name,
            'ip': plc_config.ip,
            'port': plc_config.port,
            'enabled': plc_config.enabled,
            'register_ranges': [asdict(r) for r in plc_config.register_ranges]
        }
    
    return jsonify(config)

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, shutting down...")
    if data_pool:
        data_pool.stop()
    sys.exit(0)

def main():
    """Main function"""
    global data_pool
    
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    print("PLC Data Pool Master Starting...")
    print("=" * 50)
    
    try:
        # Initialize data pool
        data_pool = PLCDataPool()
        
        # Start data collection
        data_pool.start()
        
        # Start Flask API server
        logger.info("Starting API server on http://0.0.0.0:8000")
        print("API server starting on http://localhost:8000")
        print("Health check: http://localhost:8000/api/health")
        print("Press Ctrl+C to stop")
        
        app.run(host='0.0.0.0', port=8000, debug=False, threaded=True)
        
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user")
    except Exception as e:
        logger.error(f"Error in main: {e}")
    finally:
        if data_pool:
            data_pool.stop()

if __name__ == '__main__':
    main()