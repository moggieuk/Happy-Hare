# Installer dev

This provides a quick way to run through the installer in a docker environment, making it more portable.

> [!NOTE]
> This will create/update configs at `<repo-root>/installer-dev/config`. You may then review the changes there
> or completely remove the files and start from scratch.

## Usage

### Full install

This will run the installer with `-i` which forces it to run through the questionaire.

```shell
cd ./installer-dev
docker compose run --build --rm install
```

### Upgrade

This will run the installer without `-i` which will perform a config upgrade.

```shell
cd ./installer-dev
docker compose run --build --rm upgrade
```
