# Airspace Awareness - System Architecture

## Overview

The Airspace Awareness system is designed as a modular, multi-source data fusion platform for real-time airspace monitoring.

## Components

### Capture Module (`src/capture/`)

Multi-source data acquisition:
- **WiFi Capture**: Passive sniffing via Scapy in monitor mode
- **Bluetooth Capture**: BLE scanning with Service Data 0xFFFA detection
- **ADS-B Capture**: Aircraft data polling from tar1090
- **GPS Reader**: GNSS position from gpsd or serial interface

### Remote ID Module (`src/remoteid/`)

AST F3411 compliance:
- **Parser**: Decodes remote ID broadcast messages
- **Encoder**: Generates synthetic test messages

### Fusion Engine (`src/fusion/`)

Data integration:
- Track management and deduplication
- Multi-source data correlation
- Track pruning and lifecycle management

### Airspace Module (`src/airspace/`)

Airspace intelligence:
- **Geofence Manager**: UK zones and boundary validation
- **FRZ Generator**: Flight Restricted Zone updates
- **OpenAIP Sync**: NOTAM and airspace data synchronization
- **NOTAM Import**: Manual NOTAM ingestion
- **Proximity Alert**: Real-time boundary detection

### Tiles Module (`src/tiles/`)

Map tile management:
- Hybrid online/offline tile sourcing
- Cache management and optimization

### GUI Module (`src/gui/`)

User interface:
- Kivy-based map display
- Zone information popups
- Data freshness indicators
- Settings and disclaimer screens

## Data Flow

1. **Capture** → Multi-source data acquisition
2. **Fusion** → Track correlation and deduplication
3. **Airspace** → Geofence and proximity analysis
4. **GUI** → Real-time visualization and alerts

## Configuration

See `config_reference.md` for detailed configuration options.
