# Installer dev

This provides a quick way to run through the installer in a docker environment, making it more portable.

> [!NOTE]
> This will create/update configs at `<repo-root>/installer-dev/config`. You may then review the changes there
> or completely remove the files and start from scratch.
> currently symbolic links aren't copied 

There are two targets:
- debian, to mimic most common klipper installs (mainsail OS, Raspian OS, etc)
- alpine, to mimic a busybox environment like with Creality K1 

## Usage

To run use:
```shell 
docker compose run --build --rm <target> '<command>'
```

for example, to run the installer with a debian base:

```shell
docker compose run --build --rm debian 'make install'
```


