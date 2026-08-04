# I built an AI to watch a bird. She left.

*A weekend project, an LLM, and what an empty nest box taught me about working with AI*

There's a Common Redstart that nests in my garden. The Swedes call her *rödstjärt* — red-tail. This spring I built her a nest box and put a camera inside. And then, because I'd been looking for an excuse to play seriously with vision models, I taught an AI to watch her.

It was meant to be a weekend project. It became something else.

Two weeks. Five eggs. One afternoon she flew out of the box and never came back. I don't know why. She might have moved on. More likely something happened to her — nature is cruel, and a small bird is one of the most disposable things in it.

This is what I built. What I learned. And what an empty frame taught me about working with AI.

---

## What it does

There's a camera in the nest box — a UniFi Protect, pulled over RTSP. Every five minutes the system grabs a frame and sends it to Google's Gemini. Gemini classifies what it sees: *empty nest*, *adult arrives*, *eggs visible*, *incubating*, and so on. A one-sentence narrative is attached to every event.

A motion trigger runs in parallel. The moment anything stirs inside the box, ffmpeg saves a few seconds of pre-buffered video. That clip also goes to Gemini, which describes what happened in it.

At the end of every day, a second model — Anthropic's Claude — reads everything Gemini saw that day. Frames, clips, captions, the whole stream. Claude writes a journal entry about it, in the voice of a field biologist. Each entry includes a paragraph of biological context: why this species nests this way, what a clutch of five means, why she sits with her tail angled like that.

And then a local model on a GPU box in my office reads that entry aloud — in something that sounds suspiciously like David Attenborough. It's XTTS-v2, running offline, trained on a few minutes of his voice. I asked permission of no one. The result is for an audience of one.

There's also a daily timelapse, a season-wide cumulative film updated nightly, and a species reference page with CC-licensed photography. None of which I asked for at the start. The weekend got long.

---

## The two-model trick

The single most useful thing I learned from this project is not about prompts. It's about the architectural choice of which model does what.

Gemini watches every frame in real time. Cheap, fast, perfectly suited for that job. It looks at one square photograph at a time and decides what's in it. It has no memory of yesterday. It has no idea what a season looks like. It doesn't even know what frame came before this one.

Claude sits one layer up. Once a day, it reads everything Gemini wrote that day — and writes the journal. It also corrects Gemini. When Gemini misclassifies a single frame and a day's worth of context makes the mistake obvious, Claude catches it and overrides the label.

This split is the whole story. The cheap, fast model does the work that has to happen on every frame. The expensive, context-aware model does the work that needs the bigger picture. Neither would do this job on its own. Gemini doesn't know what yesterday meant. Claude is too slow and too expensive to look at every frame for two weeks straight.

Once I'd built it this way, every other AI architecture question started to look different to me. The question isn't *which model is best?* That question is the wrong question. The questions are: which model fits which job, and what does each one need to see to do that job well.

That single insight is worth the entire project to me. I would have read it in a paper and nodded. I had to build it before it meant anything.

---

## The prompt that wouldn't behave

A bird sitting in a hole looks like a bird sitting in a hole. It's the same picture whether she's building the nest, laying an egg, or incubating. A human birder distinguishes between these by knowing the calendar, by remembering yesterday, by reading the posture in context.

Gemini cannot do any of that. It sees one frame, in isolation, with no memory. So the prompt had to do all of the work that the context would normally do — encode, in plain text, the rules a birder carries in their head.

I rewrote it twenty times. Probably more. Each version taught me something concrete about how this generation of vision models actually fails — which is, importantly, not the same as how the demos say they fail.

A few of the things I had to teach the model. That a clutch is built up over five or six days, one egg per day, before incubation begins — so a frame in mid-May with three eggs and a sitting bird is laying, not incubating. That nest-building is bursts of movement with material, and incubation is stillness for hours; the cue is duration, but you cannot see duration in a single frame, you can only see posture, and posture is ambiguous. That "absent from box" and "absent from frame" are different things, because the bird might be on the lip, just out of view, or genuinely gone.

But the rewrite that mattered most wasn't any of those. It was the one that taught the model when to say *unclear*. Every previous version of the prompt had tried to make Gemini smarter. The version that finally worked was trying to make it humbler. A model that confidently mislabels every fifth frame is worse than a model that abstains on the hard ones.

---

## When she didn't come back

She incubated for a few days. Then one afternoon she flew out of the box and never returned. The camera kept rolling. Every frame since then has looked the same: five small eggs on a nest, no bird.

Gemini classified all of those frames correctly. *No bird present.* Hundreds of frames in a row, all labeled accurately. And it missed entirely the most important thing that has ever happened in front of that camera.

Not because the prompt was bad. By that point the prompt was the cleanest piece of writing in the project. It missed it because the model could only see what was in the frame. The absence wasn't in any frame. The absence was in the relationship between today and yesterday — and a model that judges every frame in isolation cannot see relationships.

Claude could have. Claude reads a whole day at a time. But the system I'd built wasn't designed to ask *is anything missing that should be here?* It was designed to describe what was present. And the journal entries since have done exactly that — accurately, clinically, the way a real field biologist's notes might read. *No activity. Nest abandoned. Cause unknown.*

The model is doing its job. The model is doing it correctly. The model has no idea anything is wrong.

---

## What I'd actually take from this

I've been thinking about why I built this. It wasn't useful. Nobody asked for it. It will never be a product. And yet I learned more about working with AI in two weeks of watching a bird than I have from any course or paper or article this year.

The reason, I think, is that the stakes were exactly right. The project had to *work* — there's no point in a system that can't tell incubation from nest-building — but it didn't have to *succeed*. It wasn't going to be sold. It wasn't going to be reviewed. If the prompt was wrong, the only person I disappointed was me.

That's the gap that personal projects fill. Work projects have to succeed; the stakes are too high for the kind of poking around that actually teaches you how a model fails. Courses give you the curated version of the model's behavior — the version that demos well. Papers give you results, not process. A small, real, slightly pointless project gives you the actual behavior, in your hands, under conditions you can change.

So if you've been meaning to do this and haven't: pick something you love, or something you're just curious about, and let an LLM help you build it. It will fail in interesting ways. The failures are the entire point. Mine took a lot longer than a weekend. Yours probably will too. That's also the point.

The bird may come back next spring. The same female, or her daughter from a previous brood, or another *rödstjärt* looking for a box. If she does, the camera will be waiting. So will the model. And maybe by then I'll have figured out how to teach it what an absence looks like.

---

## Stack, for the curious

| Layer | Tool |
|---|---|
| Camera | UniFi Protect (RTSPS feed) |
| Capture / motion | Python + ffmpeg, frame-diff trigger |
| Frame classification | Gemini 2.5 Flash |
| Journal + correction | Claude |
| Narrator | XTTS-v2, local GPU |
| Web | FastAPI + Jinja2 + HTMX |
| Database | SQLite (WAL mode) |
| Notifications | ntfy |
| Deploy | Docker Compose, runs in my basement |

Code is open source: [github.com/andreas0480/redtail](https://github.com/andreas0480/redtail).
