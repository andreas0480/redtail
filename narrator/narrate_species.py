#!/usr/bin/env python3
"""One-off: narrate the entire Common Redstart species page.

Reads a hand-crafted speech-friendly version of the species text (numbers
spelled out, no tables, no markdown), synthesizes a single MP3 in
Attenborough's voice, and writes it to app/static/species/narration.mp3
so the static file is served directly without docker plumbing.

Run once. Re-run after editing the species page text below.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL_DIR = ROOT / "model" / "Finished_model_files"
REF_WAV = MODEL_DIR / "ref.wav"
OUT_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "species"
OUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("narrate-species")

# Re-use the chunking & synthesis logic from narrate.py
sys.path.insert(0, str(ROOT))
from narrate import _chunk, synthesize, wav_to_mp3, load_model  # noqa: E402


# Speech-friendly version of the species page. Numbers spelled out, units expanded,
# Latin names italicised in writing become spoken naturally.
SPECIES_TEXT = """
The Common Redstart, Phoenicurus phoenicurus, known as the rödstjärt in Swedish, is a small migratory songbird of the Old World flycatcher family. Its name comes from the archaic English word "start", meaning tail, and the Greek "phoinix", meaning crimson. The constant quivering of that bright orange-red tail is the bird's most distinctive field character.

Widespread across Europe and western Asia in summer, the redstart is a long-distance migrant that spends its winters in the Sahel belt of West Africa. It favours old, open woodland with natural cavities, and readily adopts nest boxes.

Adults measure between thirteen and fourteen and a half centimetres in length, with a wingspan of twenty to twenty-four centimetres. They weigh between eleven and twenty-three grams. The clutch typically contains five to seven eggs, incubated by the female for twelve to fourteen days. Fledging occurs at around sixteen days. They arrive in Sweden in late April or early May, and winter in the Sahel from Senegal to Mali.

Appearance.

The male is unmistakable in breeding plumage. The crown, nape, and mantle are slate blue-grey; the forehead bears a white blaze. Face and throat are solid black. Breast and flanks are vivid orange-rufous, grading paler towards the vent. The rump and most of the tail are the diagnostic bright orange-red. The central pair of tail feathers alone is dark brown. In fresh autumn plumage, narrow pale feather fringes give a somewhat washed-out look; these abrade away by spring to reveal full breeding colours, without a moult.

The female is considerably less striking, but still distinctive once known. She is grey-brown above, with a neat white eye-ring; underparts creamy-buff with a faint orange wash. The rump and tail match the male's orange-red coloration and are the key identification feature at a distance. Older females occasionally show a faint dark bib. Juveniles of both sexes are brown with buffy-white spotting overall, similar to young Robins, but always show the orange tail.

Distribution and habitat.

The Common Redstart breeds across the Palearctic, from Morocco and Iberia east through virtually all of Europe and temperate Asia to Lake Baikal. It is absent from Iceland and very local in Ireland. In Sweden it is a common and widespread summer visitor — one of the characteristic sounds of birch and mixed woodland from May onwards.

It is strongly associated with open, mature woodland with high horizontal visibility and a low, sparse understorey: old oak and birch forest, traditional orchard high-stem meadow landscapes, riverine alder and willow stands, and parkland with ancient trees. Key habitat features are an abundance of old trees with natural cavities or nest boxes, short grass or bare ground for foraging, and standing dead wood as song posts. It avoids dense, structurally uniform plantations.

Wintering birds occupy the Sahel belt of West Africa, roughly between five and twenty degrees north, from Senegal and The Gambia east through Mali and Burkina Faso. Geolocator studies on Danish-breeding birds show winter sites concentrated around southern Mali and northern Burkina Faso. Individual birds are largely sedentary within their winter territory, despite the highly seasonal Sahelian climate.

Migration.

The Common Redstart is one of the longer-distance European migrants, covering roughly five to six thousand kilometres between breeding and wintering grounds. Spring and autumn routes form a pronounced loop.

In spring, birds leave Africa from late February, moving broadly north through Iberia and then fanning north-east across France and central Europe towards Scandinavia. Males arrive on breeding grounds three to five days ahead of females. In Sweden the first males typically appear in late April, with most birds arriving in early May, among the later arrivals of European summer visitors.

In autumn, the species is one of the earliest migrants to depart. Scandinavian breeders leave from mid-July. The autumn route loops counter-clockwise: south-west through western Europe and down the Atlantic coast of Iberia and Morocco, then a sharp eastward turn at the Saharan edge into the Sahel interior. A strikingly different path from the direct spring return.

Breeding biology.

Males establish territories of roughly half a hectare to one hectare, singing from exposed perches at dawn and dusk. The song is a soft, melancholy warble, in three parts: a clear fluted introduction, a repetitive middle section, and a variable, sometimes jangling finale.

The male's courtship display is one of the most characteristic behaviours. He approaches the female with wings raised and rapidly trembling — known as wing-shivering — tail fanned and dipped to expose the full orange-red rump. He also leads her to candidate nest sites, repeatedly entering and singing from the entrance. The female makes the final site choice.

Only the female builds the nest, taking one to eight days. The outer cup uses dry grass, plant stems, roots, bark strips, moss, and leaves; the inner cup is lined with finer hair, wool, and feathers. One study recorded at least six hundred material-carrying trips for a single nest.

Eggs are uniform pale sky-blue, essentially unspeckled. Among the cleanest-coloured eggs of any European passerine. They measure approximately nineteen by fourteen millimetres, weighing nearly two grams. Clutch size averages just over six eggs for first broods. One egg is laid per day, always in the morning. Incubation typically does not begin until the last or penultimate egg is laid, ensuring largely synchronous hatching. Northern populations, such as those in Sweden, are usually single-brooded.

The female incubates alone for twelve to fourteen days. She spends roughly three-quarters of daylight hours on the nest, leaving for foraging breaks totalling about a quarter of the day. The male does not incubate, but remains highly active on the territory: singing from perches near the nest, maintaining the territorial boundary, and occasionally bringing food to the sitting female.

Chicks are altricial. Hatched blind, with sparse dark grey down, utterly helpless at hatching. The female broods almost continuously for the first five days. Both parents then share provisioning at increasing rates, delivering soft caterpillars, small flies, beetles, and spiders. Fledging occurs at twelve to seventeen days, usually early in the morning. Fledglings remain dependent on the parents for a further one to two weeks.

Behaviour.

The most immediately obvious field character is the constant tail-quivering. Both sexes continuously quiver the orange tail with a rapid up-and-down trembling that continues during perching, singing, foraging, and alarm. It is thought to function in communication, and may also flush invertebrate prey from vegetation.

The species is a classic perch-and-sally predator. It watches from a low branch, stone, or post, then drops to bare ground or short turf to snatch a prey item, sometimes pursuing flying insects aerially, like a flycatcher. Prey includes flies, caterpillars, beetles, ants, spiders, and earthworms.

Unusually for European songbirds, the Common Redstart is a regular host of the Common Cuckoo. Studies show that redstart chicks do not suffer markedly increased mortality when sharing a nest with a cuckoo chick. Partly, it seems, because the large cuckoo helps maintain nest temperature.

Conservation.

The species is currently listed as Least Concern by the International Union for Conservation of Nature. The European breeding population is estimated between nine and a half and fifteen million pairs. The United Kingdom population is around one hundred and thirty-five thousand pairs, where the species is on the Amber list due to long-term decline.

The European population has increased modestly since nineteen-eighty, but regional trends diverge sharply. The United Kingdom has seen a long-term decline of around ten percent since nineteen sixty-seven. Central European populations have also fallen in some regions.

The principal threats are: climate-driven drought in Sahelian wintering grounds; loss of traditional orchards and veteran trees with natural cavities; agricultural intensification, raising grass height and reducing foraging ground; and pesticide use, reducing insect prey. Nest box schemes and the retention of dead wood and old trees are among the most effective conservation interventions.
""".strip()


def main():
    text = SPECIES_TEXT
    chunks = _chunk(text)
    log.info("text: %d chars -> %d chunks", len(text), len(chunks))

    model, config = load_model()

    wav_path = OUT_DIR / "narration.wav"
    mp3_path = OUT_DIR / "narration.mp3"

    t0 = time.time()
    synthesize(model, config, text, wav_path)
    log.info("synth complete in %.1fs (%d KB wav)", time.time() - t0, wav_path.stat().st_size // 1024)

    wav_to_mp3(wav_path, mp3_path)
    wav_path.unlink(missing_ok=True)

    # Duration
    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(mp3_path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    log.info("wrote %s (%.1f KB, %s s)", mp3_path, mp3_path.stat().st_size / 1024, dur)


if __name__ == "__main__":
    main()
