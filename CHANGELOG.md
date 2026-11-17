# Changelog

## [1.5.2](https://github.com/IamMrCupp/bench-suite/compare/v1.5.1...v1.5.2) (2026-11-17)


### Bug Fixes

* **doctor:** only suggest sudo when it would show more ([6439eb7](https://github.com/IamMrCupp/bench-suite/commit/6439eb70283bbdbd9fc8ba7fa3252dd73837b3b9))

## [1.5.1](https://github.com/IamMrCupp/bench-suite/compare/v1.5.0...v1.5.1) (2026-11-17)


### Bug Fixes

* **macos:** stop a running app that declines to quit when asked ([c05d5a4](https://github.com/IamMrCupp/bench-suite/commit/c05d5a463e2faca5b1aeb4380309137b20c5384a))

## [1.5.0](https://github.com/IamMrCupp/bench-suite/compare/v1.4.1...v1.5.0) (2026-11-17)


### Features

* **macos:** set up the menu-bar apps on a Mac, and check OBS's camera extension ([096932f](https://github.com/IamMrCupp/bench-suite/commit/096932f1198c1027a20e4d36bce4da40b6f1812d))


### Documentation

* **readme:** how to run bench-setup on a Mac before the tap exists ([b6601a2](https://github.com/IamMrCupp/bench-suite/commit/b6601a2d66d90217a687303753eff527e656251c))

## [1.4.1](https://github.com/IamMrCupp/bench-suite/compare/v1.4.0...v1.4.1) (2026-11-17)


### Bug Fixes

* **cameras:** name each camera once, and explain the thermal rule only when asking ([6921bfd](https://github.com/IamMrCupp/bench-suite/commit/6921bfd7e7f38ab9d9f727bdc0baca77928a1d1e))

## [1.4.0](https://github.com/IamMrCupp/bench-suite/compare/v1.3.0...v1.4.0) (2026-11-17)


### Features

* **cameras:** guided /dev/cam-* names and the thermal autosuspend rule ([afe0995](https://github.com/IamMrCupp/bench-suite/commit/afe09952528ae86c1801e53617d3c7b0ed3e1cc2))
* **obs:** say what to point OBS at, and run every step from plain bench-setup ([b608767](https://github.com/IamMrCupp/bench-suite/commit/b608767e4e50edd7d1b46e907e22299305f62d32))

## [1.3.0](https://github.com/IamMrCupp/bench-suite/compare/v1.2.0...v1.3.0) (2026-11-17)


### Features

* **doctor:** read-only checks of the services and the host ([19a380e](https://github.com/IamMrCupp/bench-suite/commit/19a380e3b59ce55a49a50ccc51f99175dc99cb77))


### Bug Fixes

* **host:** a command that isn't installed fails, it doesn't crash ([23bab8c](https://github.com/IamMrCupp/bench-suite/commit/23bab8cdd31e3657b92c0ee091609d04159c20a9))


### Documentation

* **readme:** real install lines for the apt repo ([e92b581](https://github.com/IamMrCupp/bench-suite/commit/e92b5814dbb72c2c41aebd0d35f90a6fa10c33c3))

## [1.2.0](https://github.com/IamMrCupp/bench-suite/compare/v1.1.0...v1.2.0) (2026-11-16)


### Features

* **deb:** package bench-setup and the bench-suite metapackage ([54a0608](https://github.com/IamMrCupp/bench-suite/commit/54a0608997c9c04d57ea83c8d3cf031d06aafcb7))

## [1.1.0](https://github.com/IamMrCupp/bench-suite/compare/v1.0.0...v1.1.0) (2026-11-16)


### Features

* **services:** a container route, bench-setup services --compose ([6ba7cdf](https://github.com/IamMrCupp/bench-suite/commit/6ba7cdf28a26e9f06f45ee908f6acd1230d28afc))


### Bug Fixes

* **host:** commands never inherit bench-setup's stdin ([0eaeca3](https://github.com/IamMrCupp/bench-suite/commit/0eaeca39dc61f8f89f0f50badca3c1e6d4265aa2))

## 1.0.0 (2026-11-15)


### Features

* **services:** set up the bench services and wire benchhud to them ([6ce60e6](https://github.com/IamMrCupp/bench-suite/commit/6ce60e659a87193619b846ad01a1bae49ddd1d20))
