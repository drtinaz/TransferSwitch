This service is intended to work along side kevin windrem's exttransferswitch, which is now part of guimods.
the purpose of this service is to monitor the outdoor temperature, generator temperature, and altitude. The service then calculates a derated output for the generator based on these inputs. the base output (rated output) of the generator, the temperature derate variable, and the altitude derate variable can all be changed by editing auto_current.py. these variables are listed at the top of the script.

INSTALL
easiest way to install is using kevins setup helper. Manually add the repo using the following settings:

package name: GenAutoCurrent
github user: drtinaz
branch/tag: main
