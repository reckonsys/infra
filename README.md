# DEPRECATED

This repo is deprecated. We have moved to [reckonsys/bigga](https://github.com/reckonsys/bigga)

# infra

The Code for our infra


## Integration

* Create a `.infra.json` in required repo
* Add those names in fabfile to do a setup_env
* create guniconfig.py for django apps

### guniconfig.py

```
import sys
import multiprocessing

bind = "127.0.0.1:" + sys.argv[-1]
workers = multiprocessing.cpu_count() * 2 + 1
```
