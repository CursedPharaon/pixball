
import asyncio
import json
import math
import os
import uuid

from aiohttp import web, WSMsgType

# ---------- Константы поля ----------
W, H = 1000, 600
GOAL_H = 160
BALL_R = 10
PLAYER_R = 15
GOAL_TOP = H / 2 - GOAL_H / 2
GOAL_BOT = H / 2 + GOAL_H / 2

TICK = 1 / 30
PLAYER_ACC = 0.55
PLAYER_DAMP = 0.94
BALL_DAMP = 0.985
KICK_RANGE = PLAYER_R + BALL_R + 4
KICK_POWER = 9.0

RED = "red"
BLUE = "blue"

# Разрешённые Origin для WebSocket.
# Впиши сюда URL клиента на Render. Пустое множество = разрешить всем.
ALLOWED_ORIGINS = {
    "https://pixball.onrender.com",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
}


class Player:
    def __init__(self, pid, name, team):
        self.id = pid
        self.name = name
        self.team = team
        self.x = 150 if team == RED else W - 150
        self.y = H / 2
        self.vx = 0.0
        self.vy = 0.0
        self.input = {"up": False, "down": False, "left": False,
                      "right": False, "kick": False}
        self.kick_cd = 0

    def to_dict(self):
        return {"id": self.id, "name": self.name, "team": self.team,
                "x": round(self.x, 1), "y": round(self.y, 1)}


class Game:
    def __init__(self):
        self.players = {}
        self.ball = {"x": W / 2, "y": H / 2, "vx": 0.0, "vy": 0.0}
        self.score = {RED: 0, BLUE: 0}
        self.reset_timer = 0.0

    def add_player(self, name):
        reds = sum(1 for p in self.players.values() if p.team == RED)
        blues = sum(1 for p in self.players.values() if p.team == BLUE)
        team = RED if reds <= blues else BLUE
        pid = uuid.uuid4().hex[:8]
        self.players[pid] = Player(pid, name or f"Player{pid[:3]}", team)
        return self.players[pid]

    def remove_player(self, pid):
        self.players.pop(pid, None)

    def step(self):
        if self.reset_timer > 0:
            self.reset_timer -= TICK
            if self.reset_timer <= 0:
                self._reset_positions()
            return

        # --- игроки ---
        for p in self.players.values():
            ax = ay = 0.0
            if p.input["left"]:
                ax -= PLAYER_ACC
            if p.input["right"]:
                ax += PLAYER_ACC
            if p.input["up"]:
                ay -= PLAYER_ACC
            if p.input["down"]:
                ay += PLAYER_ACC
            if ax and ay:
                ax *= 0.7071
                ay *= 0.7071
            p.vx = (p.vx + ax) * PLAYER_DAMP
            p.vy = (p.vy + ay) * PLAYER_DAMP
            p.x += p.vx
            p.y += p.vy

            # границы поля (в зоне ворот по X пускаем дальше — но не за поле)
            p.x = max(PLAYER_R, min(W - PLAYER_R, p.x))
            p.y = max(PLAYER_R, min(H - PLAYER_R, p.y))

            if p.kick_cd > 0:
                p.kick_cd -= 1
                continue

            if p.input["kick"]:
                dx = self.ball["x"] - p.x
                dy = self.ball["y"] - p.y
                dist = math.hypot(dx, dy)
                if dist < KICK_RANGE and dist > 0.001:
                    self.ball["vx"] += (dx / dist) * KICK_POWER
                    self.ball["vy"] += (dy / dist) * KICK_POWER
                    p.kick_cd = 6

        # --- столкновения игрок-игрок ---
        plist = list(self.players.values())
        for i in range(len(plist)):
            for j in range(i + 1, len(plist)):
                a, b = plist[i], plist[j]
                dx, dy = b.x - a.x, b.y - a.y
                d = math.hypot(dx, dy)
                min_d = PLAYER_R * 2
                if 0 < d < min_d:
                    overlap = (min_d - d) / 2
                    nx, ny = dx / d, dy / d
                    a.x -= nx * overlap
                    a.y -= ny * overlap
                    b.x += nx * overlap
                    b.y += ny * overlap
                    # мягкий обмен импульсом
                    avx, avy = a.vx, a.vy
                    a.vx = b.vx * 0.5
                    a.vy = b.vy * 0.5
                    b.vx = avx * 0.5
                    b.vy = avy * 0.5

        # --- мяч ---
        b = self.ball
        b["vx"] *= BALL_DAMP
        b["vy"] *= BALL_DAMP
        b["x"] += b["vx"]
        b["y"] += b["vy"]

        # мяч-игрок
        for p in self.players.values():
            dx = b["x"] - p.x
            dy = b["y"] - p.y
            d = math.hypot(dx, dy)
            min_d = PLAYER_R + BALL_R
            if 0 < d < min_d:
                nx, ny = dx / d, dy / d
                b["x"] = p.x + nx * min_d
                b["y"] = p.y + ny * min_d
                b["vx"] = nx * 2.0 + p.vx * 0.6
                b["vy"] = ny * 2.0 + p.vy * 0.6

        # стены и голы
        if b["y"] - BALL_R < 0 and not (GOAL_TOP < b["x"] < GOAL_BOT):
            b["y"] = BALL_R
            b["vy"] *= -0.8
        if b["y"] + BALL_R > H and not (GOAL_TOP < b["x"] < GOAL_BOT):
            b["y"] = H - BALL_R
            b["vy"] *= -0.8

        if b["x"] - BALL_R < 0:
            if GOAL_TOP < b["y"] < GOAL_BOT:
                self.score[BLUE] += 1
                self._start_reset()
                return
            b["x"] = BALL_R
            b["vx"] *= -0.8

        if b["x"] + BALL_R > W:
            if GOAL_TOP < b["y"] < GOAL_BOT:
                self.score[RED] += 1
                self._start_reset()
                return
            b["x"] = W - BALL_R
            b["vx"] *= -0.8

    def _start_reset(self):
        self.reset_timer = 1.5
        self.ball = {"x": W / 2, "y": H / 2, "vx": 0.0, "vy": 0.0}
        for p in self.players.values():
            p.vx = 0.0
            p.vy = 0.0

    def _reset_positions(self):
        for p in self.players.values():
            p.x = 150 if p.team == RED else W - 150
            p.y = H / 2
            p.vx = p.vy = 0.0

    def snapshot(self):
        return {
            "type": "state",
            "ball": {k: round(v, 1) for k, v in self.ball.items()},
            "players": [p.to_dict() for p in self.players.values()],
            "score": self.score,
        }


game = Game()
clients = {}  # ws -> player_id


async def health(request):
    return web.Response(text="ok")


async def ws_handler(request):
    origin = request.headers.get("Origin", "")
    if ALLOWED_ORIGINS and origin and origin not in ALLOWED_ORIGINS:
        return web.Response(status=403, text="forbidden origin")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    player = None
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue

            t = data.get("type")
            if t == "join" and player is None:
                player = game.add_player(data.get("name", ""))
                clients[ws] = player.id
                await ws.send_json({
                    "type": "welcome",
                    "id": player.id,
                    "field": {
                        "w": W, "h": H,
                        "goalTop": GOAL_TOP, "goalBot": GOAL_BOT,
                        "ballR": BALL_R, "playerR": PLAYER_R,
                    },
                })
            elif t == "input" and player is not None:
                inp = data.get("input", {})
                for k in ("up", "down", "left", "right", "kick"):
                    player.input[k] = bool(inp.get(k, False))
    finally:
        if player is not None:
            game.remove_player(player.id)
        clients.pop(ws, None)

    return ws


async def ticker(app):
    while True:
        try:
            game.step()
            snap = game.snapshot()
            dead = []
            for ws in list(clients.keys()):
                try:
                    await ws.send_json(snap)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                clients.pop(ws, None)
        except Exception as e:
            print("ticker error:", e)
        await asyncio.sleep(TICK)


async def on_startup(app):
    app["ticker"] = asyncio.create_task(ticker(app))


async def on_cleanup(app):
    app["ticker"].cancel()
    try:
        await app["ticker"]
    except asyncio.CancelledError:
        pass


def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/ws", ws_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    port = int(os.environ.get("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
