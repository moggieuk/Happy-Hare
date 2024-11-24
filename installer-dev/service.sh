#!/usr/bin/env sh
case $1 in
restart)
	echo "Restarting service '$0'"
	;;
*)
	echo "Unknown command: $0 $1"
	exit 1
	;;
esac
