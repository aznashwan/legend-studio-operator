# Quick and dirty deployment guide

## Gitlab prereqs:

* login to Gitlab
* Go top-left to User Settings > Applications
* Create a new application with the following:
  - Name "Legend Demo"
  - Confidential = yes
  - Scopes:  openid, profile, api
  - Redirect URI: can be left blank for now
* __Save the Client and Secret IDs for later__

Redirect URIs will be set to the following once we know the service IPs:
```bash
http://$ENGINE_IP:6060/callback
http://$SDLC_IP:7070/api/auth/callback
http://$SDLC_IP:7070/api/pac4j/login/callback
http://$STUDIO_IP:8080/studio/log.in/callback
```

## Platform prereqs:

* MicroK8s with ingress enabled
* Juju installed and hooked up to MicroK8s
* empty Juju model selected and created

Install MongoDB:
```bash
juju deploy mongodb-k8s --channel=edge
```

## Fetching/building the charms:

Repos are:
* https://github.com/aznashwan/legend-database-manager
* https://github.com/aznashwan/legend-sdlc-server-operator
* https://github.com/aznashwan/legend-engine-server-operator
* https://github.com/aznashwan/legend-studio-operator

```bash
# NOTE: use `dev` branch!!!
git clone $REPO -b dev

cd charm-dir
charmcraft pack
```

## Deploying all the components:

### Common config:
Please use the following common config for all the services requiring one:
```bash
$ cat config.yml
# NOTE: replace the following with either 'legend-sdlc-server-operator'
# or 'legend-studio-operator' as needed for the other services:
legend-engine-server-operator:
        gitlab-client-id: <Gitlab App Client ID>
        gitlab-client-secret: <Gitlab App Client Sevret>
```

### Database manager:
```bash
juju --debug \
	deploy ./legend-database-manager_ubuntu-20.04-amd64.charm \
	--resource dbamn-noop-image=ubuntu:latest
juju add-relation legend-database-manager mongodb-k8s
```

### SDLC:
```bash
juju --debug \
	deploy ./legend-sdlc-server-operator_ubuntu-20.04-amd64.charm \
	--config /path/to/sdlc-config.yaml \
	--resource sdlc-image=finos/legend-sdlc-server:0.47.0

juju add-relation legend-sdlc-server-operator legend-database-manager
```

### Engine:
```bash
juju --debug \
	deploy ./legend-engine-server-operator_ubuntu-20.04-amd64.charm \
	--config /path/to/engine-config.yaml \
	--resource engine-image=finos/legend-engine-server:2.41.0

juju add-relation legend-engine-server-operator legend-database-manager
```

### Studio:
```bash
juju --debug \
	deploy ./legend-studio-operator_ubuntu-20.04-amd64.charm \
	--config /path/to/studio_config.yaml \
	--resource studio-image=finos/legend-studio:0.2.56

juju add-relation legend-studio-operator legend-database-manager
juju add-relation legend-studio-operator legend-sdlc-server-operator
juju add-relation legend-studio-operator legend-engine-server-operator
```

## End result:

Services should be reachable on their respective IPs/Ports.
Note that the ports can be modified via config.

* SDLC: 10.1.184.196:7070
* Engine: 10.1.184.202:6060
* Studio: 10.1.184.195:8080

```bash
ubuntu@nashu-vm:~/repos$ juju status
Model     Controller  Cloud/Region        Version  SLA          Timestamp
k8s-demo  micro       microk8s/localhost  2.9.11   unsupported  14:47:07Z

App                            Version  Status  Scale  Charm                          Store     Channel  Rev  OS          Address         Message
legend-database-manager                 active      1  legend-database-manager        local               17  kubernetes  10.152.183.26
legend-engine-server-operator           active      1  legend-engine-server-operator  local               12  kubernetes  10.152.183.156
legend-sdlc-server-operator             active      1  legend-sdlc-server-operator    local               39  kubernetes  10.152.183.48
legend-studio-operator                  active      1  legend-studio-operator         local                8  kubernetes  10.152.183.221
mongodb-k8s                             active      1  mongodb-k8s                    charmhub  edge       2  kubernetes  10.152.183.132

Unit                              Workload  Agent  Address       Ports  Message
legend-database-manager/0*        active    idle   10.1.184.248         Ready to be related to Legend components.
legend-engine-server-operator/0*  active    idle   10.1.184.202         Engine service has been started.
legend-sdlc-server-operator/0*    active    idle   10.1.184.196         SDLC service has been started.
legend-studio-operator/0*         active    idle   10.1.184.195         Studio service has been started.
mongodb-k8s/0*                    active    idle   10.1.184.231
```

### NOTE: remember to set the correct redirect URIs in Gitlab!!!

### Explicit ingress relation:
All components should be relate-able to the `nginx-ingress-integrator`:
```bash
juju deploy nginx-ingress-integrator
juju add-relation legend-studio-operator nginx-ingress-integrator
```
