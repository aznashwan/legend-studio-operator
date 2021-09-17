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
# NOTE: replace the following with either 'finos-legend-sdlc-k8s'
# or 'finos-legend-studio-k8s' as needed for the other services:
finos-legend-engine-k8s:
        gitlab-client-id: <Gitlab App Client ID>
        gitlab-client-secret: <Gitlab App Client Sevret>
```

### Database manager:
```bash
juju --debug \
	deploy ./finos-legend-db-k8s_ubuntu-20.04-amd64.charm \
	--resource dbamn-noop-image=ubuntu:latest
juju add-relation finos-legend-db-k8s mongodb-k8s
```

### SDLC:
```bash
juju --debug \
	deploy ./finos-legend-sdlc-k8s_ubuntu-20.04-amd64.charm \
	--config /path/to/sdlc-config.yaml \
	--resource sdlc-image=finos/legend-sdlc-server:0.47.0

juju add-relation finos-legend-sdlc-k8s finos-legend-db-k8s
```

### Engine:
```bash
juju --debug \
	deploy ./finos-legend-engine-k8s_ubuntu-20.04-amd64.charm \
	--config /path/to/engine-config.yaml \
	--resource engine-image=finos/legend-engine-server:2.41.0

juju add-relation finos-legend-engine-k8s finos-legend-db-k8s
```

### Studio:
```bash
juju --debug \
	deploy ./finos-legend-studio-k8s_ubuntu-20.04-amd64.charm \
	--config /path/to/studio_config.yaml \
	--resource studio-image=finos/legend-studio:0.2.56

juju add-relation finos-legend-studio-k8s finos-legend-db-k8s
juju add-relation finos-legend-studio-k8s finos-legend-sdlc-k8s
juju add-relation finos-legend-studio-k8s finos-legend-engine-k8s
```

## End result:

Services should be reachable on their respective IPs/Ports.
Note that the ports can be modified via config.

* SDLC: 10.1.184.196:7070
* Engine: 10.1.184.202:6060
* Studio: 10.1.184.195:8080

```bash
ubuntu@nashu-vm:~/repos$ juju status
Model   Controller  Cloud/Region        Version  SLA          Timestamp
legend  micro       microk8s/localhost  2.9.14   unsupported  13:23:36Z

App                      Version  Status  Scale  Charm                    Store     Channel  Rev  OS          Address         Message
finos-legend-db-k8s               active      1  finos-legend-db-k8s      local                1  kubernetes  10.152.183.27   
finos-legend-engine-k8s           active      1  finos-legend-engine-k8s  local                0  kubernetes  10.152.183.170  
finos-legend-sdlc-k8s             active      1  finos-legend-sdlc-k8s    local                4  kubernetes  10.152.183.100  
finos-legend-studio-k8s           active      1  finos-legend-studio-k8s  local                1  kubernetes  10.152.183.125  
mongodb-k8s                       active      1  mongodb-k8s              charmhub  edge       4  kubernetes  10.152.183.30   

Unit                        Workload  Agent  Address       Ports  Message
finos-legend-db-k8s/0*      active    idle   10.1.184.243         Ready to be related to Legend components.
finos-legend-engine-k8s/0*  active    idle   10.1.184.247         Engine service has been started.
finos-legend-sdlc-k8s/0*    active    idle   10.1.184.246         SDLC service has been started.
finos-legend-studio-k8s/0*  active    idle   10.1.184.249         Studio service has been started.
mongodb-k8s/0*              active    idle   10.1.184.237         
```

### NOTE: remember to set the correct redirect URIs in Gitlab!!!

### Explicit ingress relation:
All components should be relate-able to the `nginx-ingress-integrator`:
```bash
juju deploy nginx-ingress-integrator
juju add-relation finos-legend-studio-k8s nginx-ingress-integrator
```
