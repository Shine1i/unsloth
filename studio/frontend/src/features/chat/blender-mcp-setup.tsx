// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { openLink } from "@/lib/open-link";
import { ChevronDown } from "lucide-react";

export function BlenderMcpSetup({ onAddServer, disabled }: {
  onAddServer: () => void;
  disabled: boolean;
}) {
  return (
    <Collapsible className="rounded-xl border p-4">
      <CollapsibleTrigger className="group flex w-full items-center justify-between rounded-md text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        Set up Blender MCP
        <ChevronDown aria-hidden="true" className="size-4 text-muted-foreground transition-transform duration-200 group-data-[state=open]:rotate-180 motion-reduce:transition-none" />
      </CollapsibleTrigger>
      <CollapsibleContent className="[--duration:200ms] motion-reduce:animate-none">
        <div className="space-y-4 pt-4 text-sm leading-relaxed text-muted-foreground">
          <p>Install and start the MCP server using Blender’s official guide, then add it to Studio. Nothing is installed automatically.</p>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={() => openLink("https://projects.blender.org/lab/blender_mcp/wiki/Llama.cpp")}>MCP server guide</Button>
            <Button size="sm" disabled={disabled} onClick={onAddServer}>Add Blender server</Button>
          </div>
          <p>Default HTTP URL: <code>http://127.0.0.1:9191/</code>. Run it on the Studio backend machine, then use Test connection in the server form.</p>
          <div className="space-y-4 border-t pt-4">
            <Button size="sm" variant="outline" onClick={() => openLink("https://www.blender.org/lab/mcp-server/")}>Download Blender add-on</Button>
            <ol className="list-decimal space-y-3 pl-5">
              <li>Enable <strong className="font-medium text-foreground">Online Access</strong> in Blender Preferences → System.</li>
              <li>Drag the website’s install button into Blender to add the <strong className="font-medium text-foreground">Blender Lab</strong> repository.</li>
              <li>Drag it in again and confirm installation. Or search <strong className="font-medium text-foreground">MCP</strong> in Preferences → Get Extensions after adding the repository.</li>
            </ol>
            <p>Enable the add-on and keep Blender open for scene tools. These can run Python and change your scene; tool results may be sent to your model provider.</p>
          </div>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
