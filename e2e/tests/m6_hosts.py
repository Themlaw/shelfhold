"""The names the M6 journeys add to the stack: the plugin repository, and the address the example
plugin ships with (its own, as the plugin asks for it). Both resolve to the address that exists on
the loopback of the stack's namespace only (`fakes/m2/hosts`), behind the test authority."""

INDEX_HOST = "plugins.e2e.test"
CATALOGUE_HOST = "catalogue.exemple.invalid"
NAMES = [INDEX_HOST, CATALOGUE_HOST]
