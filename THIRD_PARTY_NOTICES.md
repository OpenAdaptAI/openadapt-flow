# Third-party notices

OpenAdapt Flow's original source code is licensed under the MIT License in
[`LICENSE`](LICENSE). Some repository and source-distribution content has a
different file-local license. Those files are not relicensed under MIT.

## openIMIS distribution configuration

The following files are adapted from the openIMIS Docker distribution:

- `benchmark/openimis_claims/compose.yml`
- `benchmark/openimis_claims/conf/nginx/openimis.conf`
- `benchmark/openimis_claims/conf/nginx/locations/backend.loc`
- `benchmark/openimis_claims/conf/nginx/locations/frontend.loc`
- `benchmark/openimis_claims/conf/nginx/variables/var.conf`

Upstream:

- Repository: <https://github.com/openimis/openimis-dist_dkr>
- Exact commit:
  [`cd6220d1f0578e56a589c47953250c2ad3d0caa5`](https://github.com/openimis/openimis-dist_dkr/tree/cd6220d1f0578e56a589c47953250c2ad3d0caa5)
- Exact upstream paths: the same `conf/nginx/...` paths listed above, without
  the local `benchmark/openimis_claims/` prefix; the combined local
  `compose.yml` is adapted from `compose.base.yml`, `compose.postgresql.yml`,
  and `compose.cache.yml`
- Upstream license: GNU Affero General Public License version 3
  (`AGPL-3.0-only`)
- Complete license copy:
  [`benchmark/openimis_claims/conf/nginx/LICENSE-AGPL-3.0.md`](benchmark/openimis_claims/conf/nginx/LICENSE-AGPL-3.0.md)

OpenAdapt adapted these configuration files for the synthetic, loopback-only
openIMIS reference environment on 2026-07-17. The local environment trims the
upstream distribution to the services required by the claims-intake reference
workflow and adds digest pinning and fail-closed local bindings.

Each adapted file carries an SPDX license identifier and exact source URLs.
The adapted files remain under `AGPL-3.0-only`; the repository's MIT license
continues to cover OpenAdapt-authored code outside file-local exceptions. A
source distribution containing both is an aggregate with these separate
license scopes.
