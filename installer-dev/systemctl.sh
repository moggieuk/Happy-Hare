#!/usr/bin/env sh
case $1 in
restart)
	echo "systemctl: Restarting service '$2'"
	;;
list-unit-files)
	echo "systemctl: Listing system unit files"
	echo "'$2'"
	;;
*)
	echo "Unknown command: $0 $1"
	exit 1
	;;
esac
