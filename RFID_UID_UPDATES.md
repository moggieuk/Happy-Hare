# RFID UID Updates

Happy Hare maintains two related values:

- **Gate RFID** — the single UID physically observed at a gate.
- **Spoolman RFIDs** — all UIDs registered to a spool, stored as a comma-separated list.

## Updating the local gate RFID

A gate's RFID is updated automatically when an NFC tag is successfully read. A shared-reader UID is applied when the corresponding gate is loaded or preloaded.

It can also be set manually:

```gcode
MMU_GATE_MAP GATE=2 RFID=AABBCCDD
```

The value must be a single, even-length hexadecimal UID. It is normalized to uppercase. Comma-separated or otherwise invalid values are ignored.

Clear the gate RFID with:

```gcode
MMU_GATE_MAP GATE=2 RFID=''
```

The RFID is also cleared when the gate is reset or becomes empty.

Spoolman synchronization never replaces the gate RFID; it remains the UID actually observed by the printer.

## Replacing a spool's RFIDs in Spoolman

Replace all RFIDs registered to a spool:

```gcode
MMU_SPOOLMAN_TAG SPOOLID=45 RFID=AABBCCDD
```

Multiple UIDs can be supplied:

```gcode
MMU_SPOOLMAN_TAG SPOOLID=45 RFID=AABBCCDD,EEFF0011
```

The spool can alternatively be identified by its assigned gate:

```gcode
MMU_SPOOLMAN_TAG GATE=2 RFID=AABBCCDD
```

## Appending an RFID in Spoolman

Add an RFID without removing those already registered:

```gcode
MMU_SPOOLMAN_TAG SPOOLID=45 RFID=EEFF0011 APPEND=1
```

Or use the spool assigned to a gate:

```gcode
MMU_SPOOLMAN_TAG GATE=2 RFID=EEFF0011 APPEND=1
```

UIDs are normalized and duplicates are removed.

## Clearing a spool's RFIDs in Spoolman

Remove every RFID registered to a spool:

```gcode
MMU_SPOOLMAN_TAG SPOOLID=45 RFID=''
```

Or clear the spool assigned to a gate:

```gcode
MMU_SPOOLMAN_TAG GATE=2 RFID=''
```

## Registering a gate's observed RFID

Register the RFID already observed at a gate against an existing spool:

```gcode
MMU_SPOOLMAN_TAG GATE=2 SPOOLID=45 REGISTER=1
```

By default, this replaces the spool's existing RFID set. Preserve existing RFIDs with:

```gcode
MMU_SPOOLMAN_TAG GATE=2 SPOOLID=45 REGISTER=1 APPEND=1
```

## Registering directly from an NFC reader

Read and resolve a tag through Spoolman:

```gcode
MMU_NFC GATE=2 REGISTER=1
```

If the UID is unknown and automatic spool creation is enabled, Happy Hare can create a new Spoolman spool from the tag metadata and register the UID.

To attach a newly scanned tag to the spool already assigned to the gate:

```gcode
MMU_NFC GATE=2 REGISTER=1 APPEND=1
```

This is useful when placing a second RFID tag on the same spool.
