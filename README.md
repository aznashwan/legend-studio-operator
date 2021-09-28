# FINOS Legend Studio Operator

## Description

The Legend Operators package the core [FINOS Legend](https://legend.finos.org)
components for quick and easy deployment of a Legend stack.

This repository contains a [Juju](https://juju.is/) Charm for
deploying the Studio, the model-centric metadata server for Legend.

The full Legend solution can be installed with the dedicated
[Legend bundle](https://charmhub.io/finos-legend-bundle).


## Usage

The Studio Operator can be deployed by running:

```sh
$ juju deploy finos-legend-studio-k8s --channel=edge
```


## Relations

The standalone Studio will initially be blocked, and will require being later
related to the [Legend Database Operator](https://github.com/aznashwan/legend-database-manager),
as well as the [Legend GitLab Integrator](https://github.com/aznashwan/finos-legend-gitlab-integrator-k8s).

```sh
$ juju deploy finos-legend-db-k8s finos-legend-gitlab-integrator-k8s
$ juju relate finos-legend-studio-k8s finos-legend-db-k8s
$ juju relate finos-legend-studio-k8s finos-legend-gitlab-integrator-k8s
# If relating to Legend components:
$ juju relate finos-legend-studio-k8s finos-legend-sdlc-k8s
$ juju relate finos-legend-studio-k8s finos-legend-engine-k8s
```

Once related to the DB/GitLab, the Studio can then be related to the
[SDLC](https://github.com/aznashwan/legend-sdlc-server-operator) and
[Engine](https://github.com/aznashwan/legend-engine-server-operator):

```sh
$ juju relate finos-legend-studio-k8s finos-legend-sdlc-k8s
$ juju relate finos-legend-studio-k8s finos-legend-engine-k8s
```

## OCI Images

This charm by default uses the latest version of the
[finos/legend-studio](https://hub.docker.com/r/finos/legend-studio) image.
