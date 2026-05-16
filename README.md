# PMIC-Measurement and Validation GUI

A Python-based GUI application developed for PMIC (Power Management IC) validation and waveform analysis using a Keysight oscilloscope.  
The project captures PMIC power-sequencing waveforms, performs measurements, performs snapshot capture, and automatically generates PDF reports.
---

## Project Objective

To develop a measurement module for PMIC validation that:

- Captures PMIC power-sequence waveforms from an oscilloscope
- Displays live waveform data in a custom GUI
- Performs waveform measurements such as:
  - DC Voltage
  - Slew Rate
  - tRAMP
  - Rising/Falling edge detection


---

## Hardware Used

- TPS65219 EVM
- Keysight EDUX1052A Oscilloscope
- DC Power Supply
- USB-A to USB-B Cable
- PC / Laptop

---

## Software Used

- Python
- Spyder IDE
- NI-VISA
- PyVISA
- PyQt5

---

## Features

- Live waveform display
- Continuous waveform updates
- Voltage measurement
- Rising/Falling edge detection
- Automatic event capture
- Snapshot capture
- Automated PDF report generation
- GUI-based visualization

---

## Libraries Used

| Section | Libraries Used | Working in Project |
|---|---|---|
| Instrument Communication | pyvisa | Connects Python to oscilloscope and transfers waveform data |
| System / File Handling | sys, os, io, datetime, time | Handles file management, timestamps, delays, and program execution |
| Numerical Processing | numpy | Performs waveform calculations and signal processing |
| GUI Creation | PyQt5.QtWidgets | Builds the graphical user interface |
| GUI Graphics / Images | PyQt5.QtGui, QImage | Displays images and captured snapshots |
| GUI Control / Timers | PyQt5.QtCore | Updates live waveform periodically |
| Waveform Plotting | matplotlib | Displays live waveform plots |
| Snapshot & PDF Generation | FigureCanvasAgg, reportlab | Captures snapshots and generates PDF reports |

---

