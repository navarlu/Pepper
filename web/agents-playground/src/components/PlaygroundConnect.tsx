import { useConfig } from "@/hooks/useConfig";
import { CLOUD_ENABLED, CloudConnect } from "../cloud/CloudConnect";
import { Button } from "./button/Button";
import { useState } from "react";
import { ConnectionMode } from "@/hooks/useConnection";
import { useToast } from "@/components/toast/ToasterProvider";

type PlaygroundConnectProps = {
  accentColor: string;
  onConnectClicked: (mode: ConnectionMode) => void;
};

const ConnectTab = ({ active, onClick, children }: any) => {
  let className = "px-2 py-1 text-sm";

  if (active) {
    className += " border-b border-cyan-500 text-cyan-500";
  } else {
    className += " text-gray-500 border-b border-transparent";
  }

  return (
    <button className={className} onClick={onClick}>
      {children}
    </button>
  );
};

const TokenConnect = ({
  accentColor,
  onConnectClicked,
}: PlaygroundConnectProps) => {
  const { setUserSettings, config } = useConfig();
  const { setToastMessage } = useToast();
  const [url, setUrl] = useState(config.settings.ws_url);
  const [token, setToken] = useState(config.settings.token);
  const [isConnecting, setIsConnecting] = useState(false);

  const buildTokenRequestBody = () => {
    const body: Record<string, any> = {};
    body.forceNewRoom = true;
    if (config.settings.room_name) {
      body.roomName = config.settings.room_name;
    }
    if (config.settings.participant_id) {
      body.participantId = config.settings.participant_id;
    }
    if (config.settings.participant_name) {
      body.participantName = config.settings.participant_name;
    }
    if (config.settings.agent_name) {
      body.agentName = config.settings.agent_name;
    }
    if (config.settings.metadata) {
      body.metadata = config.settings.metadata;
    }
    const attributesArray = Array.isArray(config.settings.attributes)
      ? config.settings.attributes
      : [];
    if (attributesArray?.length) {
      const attributes = attributesArray.reduce((acc, attr) => {
        if (attr.key) {
          acc[attr.key] = attr.value;
        }
        return acc;
      }, {} as Record<string, string>);
      body.attributes = attributes;
    }
    return body;
  };

  const requestToken = async () => {
    const response = await fetch(`/api/token`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(buildTokenRequestBody()),
    });
    if (!response.ok) {
      const errorBody = await response.text();
      throw new Error(errorBody || response.statusText);
    }
    const { accessToken, roomName } = await response.json();
    return { accessToken: accessToken as string, roomName: roomName as string };
  };

  return (
    <div className="flex left-0 top-0 w-full h-full bg-black/80 items-center justify-center text-center">
      <div className="flex flex-col gap-4 p-8 bg-gray-950 w-full text-white border-t border-gray-900">
        <div className="flex flex-col gap-2">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            className="text-white text-sm bg-transparent border border-gray-800 rounded-sm px-3 py-2"
            placeholder="wss://url"
          ></input>
          <textarea
            value={token}
            onChange={(e) => setToken(e.target.value)}
            className="text-white text-sm bg-transparent border border-gray-800 rounded-sm px-3 py-2"
            placeholder="room token..."
          ></textarea>
        </div>
        <Button
          accentColor={accentColor}
          className="w-full"
          disabled={isConnecting}
          onClick={async () => {
            setIsConnecting(true);
            let nextToken = token;
            let nextRoomName = config.settings.room_name;
            try {
              const generated = await requestToken();
              nextToken = generated.accessToken;
              nextRoomName = generated.roomName || nextRoomName;
              setToken(nextToken);
            } catch (error) {
              if (!token) {
                setToastMessage({
                  type: "error",
                  message:
                    error instanceof Error
                      ? error.message
                      : "Failed to generate a token.",
                });
                setIsConnecting(false);
                return;
              }
              setToastMessage({
                type: "info",
                message:
                  "Failed to auto-generate a token, using the manually entered token instead.",
              });
            }
            const newSettings = { ...config.settings };
            newSettings.ws_url = url;
            newSettings.token = nextToken;
            newSettings.room_name = nextRoomName;
            setUserSettings(newSettings);
            onConnectClicked("manual");
            setIsConnecting(false);
          }}
        >
          {isConnecting ? "Generating token..." : "Connect"}
        </Button>
        <a
          href="https://kitt.livekit.io/"
          className={`text-xs text-${accentColor}-500 hover:underline`}
        >
          Don’t have a URL or token? Try out our KITT example to see agents in
          action!
        </a>
      </div>
    </div>
  );
};

export const PlaygroundConnect = ({
  accentColor,
  onConnectClicked,
}: PlaygroundConnectProps) => {
  const [showCloud, setShowCloud] = useState(true);
  const copy = CLOUD_ENABLED
    ? "Connect to playground with LiveKit Cloud or manually with a URL and token"
    : "Connect to playground with a URL and token";
  return (
    <div className="flex left-0 top-0 w-full h-full bg-black/80 items-center justify-center text-center gap-2">
      <div className="min-h-[540px]">
        <div className="flex flex-col bg-gray-950 w-full max-w-[480px] rounded-lg text-white border border-gray-900">
          <div className="flex flex-col gap-2">
            <div className="px-10 space-y-2 py-6">
              <h1 className="text-2xl">Connect to playground</h1>
              <p className="text-sm text-gray-500">{copy}</p>
            </div>
            {CLOUD_ENABLED && (
              <div className="flex justify-center pt-2 gap-4 border-b border-t border-gray-900">
                <ConnectTab
                  active={showCloud}
                  onClick={() => {
                    setShowCloud(true);
                  }}
                >
                  LiveKit Cloud
                </ConnectTab>
                <ConnectTab
                  active={!showCloud}
                  onClick={() => {
                    setShowCloud(false);
                  }}
                >
                  Manual
                </ConnectTab>
              </div>
            )}
          </div>
          <div className="flex flex-col bg-gray-900/30 flex-grow">
            {showCloud && CLOUD_ENABLED ? (
              <CloudConnect accentColor={accentColor} />
            ) : (
              <TokenConnect
                accentColor={accentColor}
                onConnectClicked={onConnectClicked}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
