import datetime
import json
from dataclasses import dataclass, field
from functools import reduce
from itertools import chain
from threading import Thread
from typing import Callable, Any

import xbmc
from xbmcgui import Dialog

from addon import nas_addon
from classes.StremioAddon import Resource, StremioAddon, StremioCatalog
from classes.StremioLibrary import StremioLibrary
from classes.StremioMeta import StremioMeta
from classes.StremioStream import StremioStream
from modules.kodi_utils import (
    log,
    dataclass_to_dict,
    set_setting,
    kodi_refresh,
    get_setting,
)

API_ENDPOINT = "https://api.strem.io/api/"
DATASTORE_FILE = nas_addon.get_file_path("datastore.json")
timeout = 20

import requests.adapters


@dataclass
class StremioAPI:
    token: str = field(init=False)
    addons: list[StremioAddon] = field(init=False, default_factory=list)
    catalogs: list[StremioCatalog] = field(init=False, default_factory=list)
    metadata: dict[str, StremioMeta] = field(init=False, default_factory=dict)
    data_store: dict[str, StremioLibrary] = field(init=False, default_factory=dict)
    session: requests.Session = field(init=False, default_factory=requests.Session)
    addons_updated: datetime = field(
        init=False, default_factory=lambda: datetime.datetime.now()
    )

    def __post_init__(self):
        self.token = get_setting("stremio.token")

        adapter = requests.adapters.HTTPAdapter()
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            }
        )
        self.get_addons()
        self.get_data_store()

    @property
    def library(self) -> dict[str, StremioLibrary]:
        return self.data_store

    def _get(self, url: str, default_return=None):
        if default_return is None:
            default_return = {}
        response = None
        log(url, xbmc.LOGINFO)
        url = url.replace("/manifest.json", "")
        try:
            response = self.session.get(url, timeout=timeout)
            return response.json()
        except Exception as e:
            log(str(e), xbmc.LOGERROR)
            if response:
                log(str(response), xbmc.LOGERROR)
            return default_return

    def _post(self, url: str, post_data=None, default_return=None):
        if default_return is None:
            default_return = {}
        if post_data is None:
            post_data = {}
        response = None
        if self.token:
            post_data["authKey"] = self.token
        try:
            log(url, xbmc.LOGINFO)
            response = self.session.post(
                f"{API_ENDPOINT}{url}", json=post_data, timeout=timeout
            )
            return response.json().get("result", {})
        except Exception as e:
            log(str(e), xbmc.LOGERROR)
            if response:
                log(str(response), xbmc.LOGERROR)
            return default_return

    def _filter_addons(
        self, addon_type: str, media_type: str, id: str = None, refresh=False
    ) -> list[StremioAddon]:
        matching_addons = []
        addons = self.get_addons(refresh)
        for a in addons:
            m = a.manifest
            if (
                addon_type in m.resources
                and (media_type in m.types or media_type is None)
                and (
                    None in [m.idPrefixes, id]
                    or any(id.startswith(i) for i in m.idPrefixes)
                )
            ):
                matching_addons.append(a)
                continue

            for r in [r for r in m.resources if type(r) == Resource]:
                if (
                    r.name == addon_type
                    and (media_type in r.types or media_type is None)
                    and (
                        None in [r.idPrefixes, id]
                        or any(id.startswith(i) for i in r.idPrefixes)
                    )
                ):
                    matching_addons.append(a)
        return matching_addons

    # TODO qr code login
    def login(self):
        username = Dialog().input("Email")
        if not username:
            return
        password = Dialog().input("Password")
        if not password:
            return
        post_data = {"email": username, "password": password}
        result = self._post("login", post_data)
        set_setting("stremio.token", result["authKey"])
        set_setting("stremio.user", result["user"]["email"])
        self.token = get_setting("stremio.token")
        kodi_refresh()

    def logout(self):
        self._post("logout")
        set_setting("stremio.token", "")
        set_setting("stremio.user", "")
        self.token = get_setting("stremio.token")

        import os

        os.remove(DATASTORE_FILE)

    def get_addons(self, refresh: bool = False) -> list[StremioAddon]:
        if (
            not self.addons
            or refresh
            or (datetime.datetime.now() - self.addons_updated).total_seconds() > 300
        ):
            self.addons_updated = datetime.datetime.now()
            response = self._post("addonCollectionGet", {"update": True})
            self.addons = StremioAddon.from_list(response.get("addons", []))
        return self.addons

    def get_catalogs(self, refresh: bool = False) -> list[StremioCatalog]:
        if not self.catalogs or refresh:
            addons = self.get_addons(refresh)
            self.catalogs = list(chain(*[a.manifest.catalogs for a in addons]))
        return self.catalogs

    def write_data_store(self):

        with open(DATASTORE_FILE, "w") as f:
            json.dump([dataclass_to_dict(i) for i in self.data_store.values()], f)

    def load_data_store(self):
        try:
            with open(DATASTORE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log(str(e), xbmc.LOGERROR)
            return None

    def get_data_store(self, refresh: bool = False) -> dict[str, StremioLibrary]:
        if not self.data_store or refresh:
            cached_store = self.load_data_store()
            response = cached_store
            if not response or refresh:
                response = self._post(
                    "datastoreGet", {"all": True, "collection": "libraryItem"}
                )
            self.data_store = {i.id: i for i in StremioLibrary.from_list(response)}
            if cached_store and not refresh:
                self.update_data_store()
            self.write_data_store()
        return self.data_store

    def update_data_store(self):
        data_store = self.data_store
        meta = self._post("datastoreMeta", {"collection": "libraryItem"})
        outdated_ids = []
        for i in meta:
            if i[0] not in data_store or data_store[
                i[0]
            ].modified_time < datetime.datetime.fromtimestamp(
                i[1] / 1000, datetime.timezone.utc
            ):
                outdated_ids.append(i[0])

        if not len(outdated_ids):
            return

        self.get_data_by_ids(outdated_ids)
        self.write_data_store()

    def get_data_by_ids(self, ids: list[str]):
        response = self._post(
            "datastoreGet",
            {"ids": ids, "collection": "libraryItem"},
        )
        log(response)
        for i in StremioLibrary.from_list(response):
            self.data_store[i.id] = i

    def get_data_by_meta(self, meta: StremioMeta) -> StremioLibrary | None:
        data_store = self.get_data_store()
        return (
            data_store[meta.id]
            if meta.id in data_store
            else StremioLibrary.from_dict({"_id": meta.id, **dataclass_to_dict(meta)})
        )

    def set_data(self, data: StremioLibrary):
        self.data_store[data.id] = data
        self.write_data_store()
        post_data = {"collection": "libraryItem", "changes": [dataclass_to_dict(data)]}
        self._post("datastorePut", post_data)

    def get_library_types(self) -> list[str]:
        types = []
        for k, v in self.get_data_store().items():
            if v.type not in types and not (v.removed or v.temp):
                types.append(v.type)
        types.sort(key=lambda c: (0 if c == "movie" else 1 if c == "series" else 2, c))
        types.insert(0, "all")
        return types

    def get_library(self, type_filter: str | None = None) -> list[StremioMeta]:
        responses = [
            StremioMeta.from_dict({"id": i.id, **dataclass_to_dict(i)})
            for i in sorted(
                [v for k, v in self.get_data_store().items() if not v.removed],
                key=lambda e: e.state.lastWatched,
                reverse=True,
            )
            if type_filter is None or i.type == type_filter
        ]
        return responses

    def get_metadata_by_libraries(
        self, libraries: list[StremioLibrary]
    ) -> list[StremioMeta]:
        responses = []

        def _get_meta(item: StremioLibrary, idx):
            responses[idx] = self.get_metadata_by_id(item.id, item.type)

        responses.extend(None for _ in libraries)
        threads = [
            Thread(target=_get_meta, args=(item, idx))
            for idx, item in enumerate(libraries)
        ]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return [r for r in responses if r]

    def get_metadata_by_id(
        self, id: str, media_type: str, refresh=False
    ) -> StremioMeta:
        responses = []

        def _get_meta(item: StremioAddon, idx: int):
            response = self._get(f"{item.base_url}/meta/{media_type}/{id}.json")
            responses[idx] = response.get("meta", {})

        if id not in self.metadata or refresh:
            meta_addons = list(self._filter_addons("meta", media_type, id))
            responses.extend(None for _ in meta_addons)

            threads = [
                Thread(target=_get_meta, args=(item, idx))
                for idx, item in enumerate(meta_addons)
            ]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.metadata[id] = StremioMeta.from_dict(
                reduce(lambda a, b: {**b, **a}, responses)
            )
        return self.metadata[id]

    def get_streams_by_id(
        self,
        id: str,
        media_type: str,
        callback: Callable[[list[StremioStream], int, int], Any],
    ) -> list[Thread]:

        stream_addons = self._filter_addons("stream", media_type, id, True)

        def _get_stream(item: StremioAddon, idx: int):
            response = self._get(f"{item.base_url}/stream/{media_type}/{id}.json")
            streams = StremioStream.from_list(response.get("streams", []))
            callback(streams, idx, len(stream_addons))

        threads = [
            Thread(target=_get_stream, args=(item, idx))
            for idx, item in enumerate(stream_addons)
        ]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return threads

    # not really used to get subtitles at the moment just for pinging syncribullet and co
    def get_subtitles_by_id(self, id: str, media_type: str) -> list[dict]:
        responses: list[dict] = []

        def _get_subs(item: StremioAddon, idx: int):
            response = self._get(f"{item.base_url}/subtitles/{media_type}/{id}.json")
            responses.append(response.get("subtitles", {}))

        sub_addons = self._filter_addons("subtitles", media_type, id)
        threads = [
            Thread(target=_get_subs, args=(item, idx))
            for idx, item in enumerate(sub_addons)
        ]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return list(chain(*responses))

    def get_home_catalogs(self) -> list[StremioCatalog]:
        log("Getting home catalogs")
        return [
            c
            for c in self.get_catalogs()
            if not any(e.isRequired for e in c.extra) and len(c.extraRequired) == 0
        ]

    def get_discover_catalogs(self) -> list[StremioCatalog]:
        return [
            c
            for c in self.get_catalogs()
            if (any(e.name == "genre" for e in c.extra) or "genre" in c.extraSupported)
            and not any(e != "genre" for e in c.extraRequired)
        ]

    def get_discover_types(self) -> list[str]:
        types = []
        catalogs = self.get_discover_catalogs()
        for catalog in catalogs:
            if catalog.type not in types:
                types.append(catalog.type)
        types.sort(key=lambda c: (0 if c == "movie" else 1 if c == "series" else 2, c))
        return types

    def get_discover_catalogs_by_type(self, catalog_type: str) -> list[StremioCatalog]:
        return [c for c in self.get_discover_catalogs() if c.type == catalog_type]

    def get_search_catalogs(self) -> list[StremioCatalog]:
        return [
            c
            for c in self.get_catalogs()
            if (
                any(e.name == "search" for e in c.extra) or "search" in c.extraSupported
            )
            and not any(e != "search" for e in c.extraRequired)
        ]

    def get_notification_catalogs(self) -> list[StremioCatalog]:
        return [
            c
            for c in self.get_catalogs()
            if (
                any(e.name == "lastVideosIds" for e in c.extra)
                or "lastVideosIds" in c.extraSupported
            )
            and not any(e != "lastVideosIds" for e in c.extraRequired)
        ]

    def get_notifications(self, library_items: list[StremioLibrary]):
        responses = []

        def _get_notification_catalog(catalog: StremioCatalog, idx: int):
            ids = ",".join(
                l.id
                for l in library_items
                if any(
                    l.id.startswith(prefix)
                    for prefix in catalog.addon.manifest.idPrefixes
                )
            )
            responses[idx] = self.get_catalog(
                catalog,
                notification_ids=f"lastVideosIds={ids}",
            )

        catalogs = self.get_notification_catalogs()

        responses.extend(None for _ in catalogs)

        threads = [
            Thread(target=_get_notification_catalog, args=(item, idx))
            for idx, item in enumerate(catalogs)
        ]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return list(chain(*responses))

    def get_catalog(
        self,
        catalog: StremioCatalog,
        genre: str = None,
        search: str = None,
        notification_ids=None,
    ) -> list[StremioMeta | None]:
        responses: list[StremioMeta | None] = []

        self.update_data_store()
        query = "/".join(
            filter(
                None,
                [
                    catalog.addon.base_url,
                    "catalog",
                    catalog.type,
                    catalog.id,
                    genre,
                    search,
                    notification_ids,
                ],
            )
        )

        response = self._get(f"{query}.json").get("metas", [])
        responses.extend(None for _ in response)

        for idx, r in enumerate(response):
            responses[idx] = StremioMeta.from_dict(r)

        return responses

    def send_events(self, events):
        return self._post("events", {"events": events})


stremio_api = StremioAPI()
