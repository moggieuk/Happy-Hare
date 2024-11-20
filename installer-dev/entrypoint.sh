#!/usr/bin/env sh

set -x

# there needs to be at least 1 line in printer.cfg for the installer to be able to add the includes
if [ ! -f "${HOME}/printer_data/config/printer.cfg" ]; then
    echo '# Printer Config' >> "${HOME}/printer_data/config/printer.cfg"
fi
cd ~/Happy-Hare

# run all the arguments as the command
exec "$@"
