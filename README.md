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
- Voltage,slew rate and tramp measurement
- Rising/Falling edge detection
- Automatic event capture
- Snapshot capture
- Automated PDF report generation
- GUI-based validation

## Benefits and Industrial Relevance

The PMIC Validation GUI is developed to simplify and accelerate oscilloscope-based PMIC validation activities by integrating waveform acquisition, visualization, measurement, snapshot capture, and report generation within a single software platform. In traditional validation workflows, engineers often perform waveform monitoring, manual measurements, image capturing, and documentation separately, which increases validation time and introduces the possibility of inconsistencies during repetitive testing procedures.

By establishing communication with the oscilloscope through VISA-based instrument control, the software enables automated waveform acquisition and real-time signal visualization. This helps validation engineers analyze PMIC power-sequencing behavior more efficiently while reducing dependency on repetitive manual operations. The integration of automatic snapshot capture and PDF report generation further improves documentation quality and minimizes the effort required for maintaining validation records.

The software also enhances measurement consistency by providing a centralized environment for waveform analysis and reporting. Such an approach improves productivity during large-scale validation activities, reduces human error in measurement recording, and supports faster debugging and verification of PMIC power rails. Overall, the developed GUI demonstrates how software-assisted validation can improve efficiency, repeatability, and documentation quality in embedded power-management testing applications.

