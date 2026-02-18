import { NextApiRequest, NextApiResponse } from "next";
import { generateRandomAlphanumeric } from "@/lib/util";
import { promises as fs } from "fs";
import path from "path";

import { AccessToken } from "livekit-server-sdk";
import { RoomAgentDispatch, RoomConfiguration } from "@livekit/protocol";
import type { AccessTokenOptions, VideoGrant } from "livekit-server-sdk";
import { TokenResult } from "../../lib/types";

const apiKey = process.env.LIVEKIT_API_KEY;
const apiSecret = process.env.LIVEKIT_API_SECRET;
const tokenLogFile =
  process.env.TOKEN_LOG_FILE || path.join(process.cwd(), "token-log.txt");
const latestSessionFile =
  process.env.LIVEKIT_SESSION_FILE ||
  path.join(process.cwd(), "token-latest.json");
const listenerIdentity =
  process.env.LISTENER_IDENTITY || "listener-python";
const listenerName = process.env.LISTENER_NAME || listenerIdentity;
const agentParticipantIdentity =
  process.env.AGENT_IDENTITY || "agent-user";
const agentParticipantName =
  process.env.AGENT_NAME || agentParticipantIdentity;
const serverLiveKitUrl =
  process.env.NEXT_PUBLIC_LIVEKIT_URL ||
  process.env.LIVEKIT_URL ||
  "ws://127.0.0.1:7880";

const createToken = (
  userInfo: AccessTokenOptions,
  grant: VideoGrant,
  agentName?: string,
) => {
  const at = new AccessToken(apiKey, apiSecret, userInfo);
  at.addGrant(grant);
  if (agentName) {
    at.roomConfig = new RoomConfiguration({
      agents: [
        new RoomAgentDispatch({
          agentName: agentName,
          metadata: '{"user_id": "12345"}',
        }),
      ],
    });
  }
  return at.toJwt();
};

const logTokenToFile = async ({
  token,
  roomName,
  identity,
  kind,
}: {
  token: string;
  roomName: string;
  identity: string;
  kind: "primary" | "listener" | "agent" | string;
}) => {
  try {
    const line = JSON.stringify({
      generatedAt: new Date().toISOString(),
      token,
      roomName,
      identity,
      kind,
    });
    await fs.appendFile(tokenLogFile, `${line}\n`, { encoding: "utf8" });
  } catch (err) {
    console.error("Failed to write token log file", err);
  }
};

const writeSessionSnapshot = async (snapshot: Record<string, unknown>) => {
  try {
    const tmpPath = `${latestSessionFile}.tmp`;
    await fs.writeFile(tmpPath, JSON.stringify(snapshot, null, 2), {
      encoding: "utf8",
    });
    await fs.rename(tmpPath, latestSessionFile);
  } catch (err) {
    console.error("Failed to write session snapshot file", err);
  }
};

const buildGrant = (
  roomName: string,
  opts?: Partial<VideoGrant>,
): VideoGrant => ({
  room: roomName,
  roomJoin: true,
  canPublish: false,
  canPublishData: true,
  canSubscribe: true,
  canUpdateOwnMetadata: true,
  ...(opts || {}),
});

export default async function handleToken(
  req: NextApiRequest,
  res: NextApiResponse,
) {
  try {
    if (req.method !== "POST") {
      res.setHeader("Allow", "POST");
      res.status(405).end("Method Not Allowed");
      return;
    }
    if (!apiKey || !apiSecret) {
      res.statusMessage = "Environment variables aren't set up correctly";
      res.status(500).end();
      return;
    }

    const {
      roomName: roomNameFromBody,
      participantName: participantNameFromBody,
      participantId: participantIdFromBody,
      metadata: metadataFromBody,
      attributes: attributesFromBody,
      agentName: agentNameFromBody,
      forceNewRoom: forceNewRoomFromBody,
    } = req.body;

    const forceNewRoom =
      forceNewRoomFromBody === undefined ? true : Boolean(forceNewRoomFromBody);

    // Generate a fresh room by default. Reuse only when explicitly requested.
    const requestedRoomName = (roomNameFromBody as string) || "";
    const roomName =
      !forceNewRoom && requestedRoomName
        ? requestedRoomName
        :
      `room-${generateRandomAlphanumeric(4)}-${generateRandomAlphanumeric(4)}`;

    // Get participant name from query params or generate random one
    const identity =
      (participantIdFromBody as string) ||
      `identity-${generateRandomAlphanumeric(4)}`;

    // Get agent name from query params or use none (automatic dispatch)
    const agentName = (agentNameFromBody as string) || undefined;

    // Get metadata and attributes from query params
    const metadata = metadataFromBody as string | undefined;
    const attributes =
      typeof attributesFromBody === "object" && attributesFromBody !== null
        ? (attributesFromBody as Record<string, string>)
        : {};

    const participantName = participantNameFromBody || identity;

    const primaryGrant = buildGrant(roomName, { canPublish: true });
    const listenerGrant = buildGrant(roomName, { canPublish: false });
    const agentGrant = buildGrant(roomName, { canPublish: true });

    const token = await createToken(
      { identity, metadata, attributes, name: participantName },
      primaryGrant,
      agentName,
    );
    const listenerToken = await createToken(
      { identity: listenerIdentity, metadata, attributes, name: listenerName },
      listenerGrant,
      agentName,
    );
    const agentParticipantToken = await createToken(
      {
        identity: agentParticipantIdentity,
        metadata,
        attributes,
        name: agentParticipantName,
      },
      agentGrant,
      agentName,
    );

    await logTokenToFile({
      token,
      roomName,
      identity,
      kind: "primary",
    });
    await logTokenToFile({
      token: listenerToken,
      roomName,
      identity: listenerIdentity,
      kind: "listener",
    });
    await logTokenToFile({
      token: agentParticipantToken,
      roomName,
      identity: agentParticipantIdentity,
      kind: "agent",
    });
    await writeSessionSnapshot({
      generatedAt: new Date().toISOString(),
      roomName,
      wsUrl: serverLiveKitUrl,
      source: "api/token",
      user: {
        identity,
        token,
      },
      listener: {
        identity: listenerIdentity,
        token: listenerToken,
      },
      agent: {
        identity: agentParticipantIdentity,
        token: agentParticipantToken,
      },
    });
    const result: TokenResult = {
      identity,
      accessToken: token,
      roomName,
      listenerIdentity,
      listenerToken,
      agentIdentity: agentParticipantIdentity,
      agentToken: agentParticipantToken,
    };

    res.status(200).json(result);
  } catch (e) {
    res.statusMessage = (e as Error).message;
    res.status(500).end();
  }
}
