# PLC Data Pool Integration Project

## Overview
Refactor SLAP1 Casting Output application to use centralized PLC data pool instead of direct connections.

## Problem
Multiple applications connecting to same PLCs cause "Connection in use" errors.

## Solution
- Master program (plc_master.py) handles all PLC connections
- Applications use HTTP API instead of direct connections
- Eliminates connection conflicts

## Files
- `plc_master.py` - Master data pool program (port 8000)
- `app.py` - SLAP1 application to be modified (port 5111)

## Architecture
Master → PLCs → HTTP API → Applications → Database