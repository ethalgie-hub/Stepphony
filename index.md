# Sheet Music to Stepper Motor Instrument

## Objects to complete

### 1. Physical Device

#### 1.1 Obtain the Parts
- ESP32 or Arduino Uno.
- Six stepper motors.
- Six compatible motor drivers.
- Suitable external power supply.
- Wires, connectors, and breadboard.
- Frame or mounting system.

#### 1.2 Build
- Assemble and test a one-motor setup.
- Expand to six motors.
- Mount the components and organize the wiring.

#### 1.3 Test and Calibrate
- Determine each motor’s playable pitch range.
- Test multiple motors playing simultaneously.

### 2. Python Program

#### 2.1 File Input and Validation
- Ask for the filename.
- Check that the file exists.
- Identify its extension.
- Reject unsupported formats.

#### 2.2 Convert the Input to MIDI
- **JPG/JPEG/PNG:** Recognize the sheet music and generate MIDI.
- **PDF:** Convert pages to images, recognize the music, and generate one MIDI file in page order.
- **MID/MIDI:** Use directly.
- Preserve timing, chords, and simultaneous parts during conversion.

#### 2.3 Read the MIDI
- Extract pitches, start times, and durations.
- Account for tempo changes.

#### 2.4 Motor Assignment and Optimization
- Assign notes to the six motors.
- Account for each motor’s playable pitch range.
- Handle passages requiring more than six simultaneous notes.

#### 2.5 Transfer Playback Data
- Format the assigned notes and timing.
- Send the playback data to the controller through USB serial.

### 3. Arduino / ESP32 Firmware

#### 3.1 Receive Playback Data
- Read and validate the data sent by Python.
- Store or buffer the events for playback.

#### 3.2 Schedule the Notes
- Start and stop notes at the correct times.
- Keep all six motors synchronized.

#### 3.3 Drive the Motors
- Convert pitches into step-pulse frequencies.
- Control multiple motors simultaneously.

#### 3.4 Playback Controls
- Implement start and stop.
- Optionally add pause, restart, and tempo adjustment.

### 4. Integration and Testing

1. Play a manually programmed note on one motor.
2. Play a manually programmed melody.
3. Play simultaneous notes on multiple motors.
4. Send a simple melody from Python to the controller.
5. Read an existing MIDI file, assign its notes to motors, and play it.
6. Convert a simple sheet-music image to MIDI and play it.
7. Test chords and multiple staves.
8. Test multipage PDFs and the complete input-to-playback workflow.

## Videos That helped me

- Converting PDFs to images:    https://www.youtube.com/watch?v=vv_4rcfzTio

## Libraries used

1. colorama
    - changes the colors of the printed text used for error messages
2. pymupdf
    - converts pdf files to images
3. os
    - Delete and create folders
4. shutil   
    - copy files
5. mido
6. pygame-ce
7. pyfluidsynth
8. numpy