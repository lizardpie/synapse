# Copyright 2019 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This class implements the proposed relation APIs from MSC 1849.

Since the MSC has not been approved all APIs here are unstable and may change at
any time to reflect changes in the MSC.
"""

import logging
from typing import TYPE_CHECKING, Optional, Tuple

from synapse.api.constants import RelationTypes
from synapse.api.errors import SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_integer, parse_string
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.storage.relations import AggregationPaginationToken
from synapse.types import JsonDict, StreamToken

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class RelationPaginationServlet(RestServlet):
    """API to paginate relations on an event by topological ordering, optionally
    filtered by relation type and event type.
    """

    PATTERNS = client_patterns(
        "/rooms/(?P<room_id>[^/]*)/relations/(?P<parent_id>[^/]*)"
        "(/(?P<relation_type>[^/]*)(/(?P<event_type>[^/]*))?)?$",
        releases=(),
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self._relations_handler = hs.get_relations_handler()

    async def on_GET(
        self,
        request: SynapseRequest,
        room_id: str,
        parent_id: str,
        relation_type: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)

        limit = parse_integer(request, "limit", default=5)
        direction = parse_string(
            request, "org.matrix.msc3715.dir", default="b", allowed_values=["f", "b"]
        )
        from_token_str = parse_string(request, "from")
        to_token_str = parse_string(request, "to")

        # Return the relations
        from_token = None
        if from_token_str:
            from_token = await StreamToken.from_string(self.store, from_token_str)
        to_token = None
        if to_token_str:
            to_token = await StreamToken.from_string(self.store, to_token_str)

        result = await self._relations_handler.get_relations(
            requester=requester,
            event_id=parent_id,
            room_id=room_id,
            relation_type=relation_type,
            event_type=event_type,
            limit=limit,
            direction=direction,
            from_token=from_token,
            to_token=to_token,
        )

        return 200, result


class RelationAggregationPaginationServlet(RestServlet):
    """API to paginate aggregation groups of relations, e.g. paginate the
    types and counts of the reactions on the events.

    Example request and response:

        GET /rooms/{room_id}/aggregations/{parent_id}

        {
            chunk: [
                {
                    "type": "m.reaction",
                    "key": "👍",
                    "count": 3
                }
            ]
        }
    """

    PATTERNS = client_patterns(
        "/rooms/(?P<room_id>[^/]*)/aggregations/(?P<parent_id>[^/]*)"
        "(/(?P<relation_type>[^/]*)(/(?P<event_type>[^/]*))?)?$",
        releases=(),
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.event_handler = hs.get_event_handler()

    async def on_GET(
        self,
        request: SynapseRequest,
        room_id: str,
        parent_id: str,
        relation_type: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)

        await self.auth.check_user_in_room_or_world_readable(
            room_id,
            requester.user.to_string(),
            allow_departed_users=True,
        )

        # This checks that a) the event exists and b) the user is allowed to
        # view it.
        event = await self.event_handler.get_event(requester.user, room_id, parent_id)
        if event is None:
            raise SynapseError(404, "Unknown parent event.")

        if relation_type not in (RelationTypes.ANNOTATION, None):
            raise SynapseError(
                400, f"Relation type must be '{RelationTypes.ANNOTATION}'"
            )

        limit = parse_integer(request, "limit", default=5)
        from_token_str = parse_string(request, "from")
        to_token_str = parse_string(request, "to")

        # Return the relations
        from_token = None
        if from_token_str:
            from_token = AggregationPaginationToken.from_string(from_token_str)

        to_token = None
        if to_token_str:
            to_token = AggregationPaginationToken.from_string(to_token_str)

        pagination_chunk = await self.store.get_aggregation_groups_for_event(
            event_id=parent_id,
            room_id=room_id,
            event_type=event_type,
            limit=limit,
            from_token=from_token,
            to_token=to_token,
        )

        return 200, await pagination_chunk.to_dict(self.store)


class RelationAggregationGroupPaginationServlet(RestServlet):
    """API to paginate within an aggregation group of relations, e.g. paginate
    all the 👍 reactions on an event.

    Example request and response:

        GET /rooms/{room_id}/aggregations/{parent_id}/m.annotation/m.reaction/👍

        {
            chunk: [
                {
                    "type": "m.reaction",
                    "content": {
                        "m.relates_to": {
                            "rel_type": "m.annotation",
                            "key": "👍"
                        }
                    }
                },
                ...
            ]
        }
    """

    PATTERNS = client_patterns(
        "/rooms/(?P<room_id>[^/]*)/aggregations/(?P<parent_id>[^/]*)"
        "/(?P<relation_type>[^/]*)/(?P<event_type>[^/]*)/(?P<key>[^/]*)$",
        releases=(),
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self._relations_handler = hs.get_relations_handler()

    async def on_GET(
        self,
        request: SynapseRequest,
        room_id: str,
        parent_id: str,
        relation_type: str,
        event_type: str,
        key: str,
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)

        if relation_type != RelationTypes.ANNOTATION:
            raise SynapseError(400, "Relation type must be 'annotation'")

        limit = parse_integer(request, "limit", default=5)
        from_token_str = parse_string(request, "from")
        to_token_str = parse_string(request, "to")

        from_token = None
        if from_token_str:
            from_token = await StreamToken.from_string(self.store, from_token_str)
        to_token = None
        if to_token_str:
            to_token = await StreamToken.from_string(self.store, to_token_str)

        result = await self._relations_handler.get_relations(
            requester=requester,
            event_id=parent_id,
            room_id=room_id,
            relation_type=relation_type,
            event_type=event_type,
            aggregation_key=key,
            limit=limit,
            from_token=from_token,
            to_token=to_token,
        )

        return 200, result


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    RelationPaginationServlet(hs).register(http_server)
    RelationAggregationPaginationServlet(hs).register(http_server)
    RelationAggregationGroupPaginationServlet(hs).register(http_server)
