#!/usr/bin/env sh

sudo chown -R klippy:klippy "${PRINTER_DATA}"
sudo chmod -R 775 "${PRINTER_DATA}"

if [ ! -f "${PRINTER_DATA}/config/moonraker.conf" ]; then
    touch "${PRINTER_DATA}/config/moonraker.conf"
fi

# run all the arguments as the command
sh -c "$@"
