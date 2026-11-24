# InputPlumber interface fixtures

The `*.xml` files are D-Bus interface descriptions captured from a running
InputPlumber 0.82.0 with `busctl introspect` (system bus, name
`org.shadowblip.InputPlumber`): the manager, one composite device and its
gamepad, mouse, keyboard and D-Bus target objects. The documentation comments
were removed; what is left are the names and signatures of a public
interface. They are used only to build the fake service in
`tests/fakes/fake_inputplumber.py`.

InputPlumber is GPL-3.0. Nothing else from it is in this repository: no
source code, device configurations, profiles or schemas.

`bus.conf` is the configuration of the private `dbus-daemon` the tests start.
It has no service directories, so nothing can be activated on that bus, and
the tests give it an abstract socket address, so no file is created.
