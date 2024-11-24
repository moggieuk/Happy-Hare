#!/usr/bin/env sh

set -x

sudo chown -R 1000:1000 "${PRINTER_DATA}"

# there needs to be at least 1 line in printer.cfg for the installer to be able to add the includes
if [ ! -f "${PRINTER_DATA}/config/printer.cfg" ]; then
    echo '# Printer Config' >>"${PRINTER_DATA}/config/printer.cfg"
fi

if [ ! -f "${PRINTER_DATA}/config/moonraker.conf" ]; then
    touch "${PRINTER_DATA}/config/moonraker.conf"
fi

# run all the arguments as the command
sh -c "$@"
