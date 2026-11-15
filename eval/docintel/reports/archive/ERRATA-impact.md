# Evaluation impact of the v1.0 -> v1.1 label errata

Generated 2026-11-14 by comparing `reports/archive/dataset-v1.0-latest.json` with `reports/latest.json` (same code except the errata changes, same Docker image, Tesseract 5.5.0). See `../../ERRATA.md`.

```
field slots                    360        -> 376        CHANGED
F1 all                         0.9856     -> 0.9848     CHANGED
exact match all                0.975      -> 0.9734     CHANGED
precision all                  0.9971     -> 0.9972     CHANGED
recall all                     0.9744     -> 0.9727     CHANGED
F1 dev                         0.995      -> 0.9952     CHANGED
F1 test                        0.973      -> 0.9709     CHANGED
F1 contract                    0.9806     -> 0.9795     CHANGED
F1 invoice                     0.991      -> 0.991      
F1 hr                          0.9683     -> 0.9658     CHANGED
governing_law F1               1.0        -> 1.0        
value_period F1                None       -> 0.9655     CHANGED
contract_value F1              0.9655     -> 0.9655     
fields with value              519        -> 533        CHANGED
evidence support               1.0        -> 1.0        
page attribution               0.9961     -> 0.9962     CHANGED
line-item cells                1.0        -> 1.0        
numeric reconciliation         0.9286     -> 0.9286     
invalid-field P                0.8        -> 0.8        
OCR CER                        0.0015     -> 0.0015     
OCR key tokens                 0.9917     -> 0.9917     
classification                 1.0        -> 1.0        
correction rate                0.0242     -> 0.0254     CHANGED
field slots (review)           495        -> 511        CHANGED
approval consistency           1.0        -> 1.0        
false-accept                   0.0        -> 0.0        
errors not flagged             0.4        -> 0.4        
approvals needing override     0          -> 0          
queries exact before review    5          -> 5          
queries exact after review     6          -> 6          
security checks                1946       -> 1946       
security violations            0          -> 0          
```

Reading: +16 slots = the new value_period field on 16 contracts (scored as a scalar field). Its only miss is di-c13 (held-out test document; its label *Mjesečna najamnina* is intentionally not in the rules lexicon), which lowers test F1 from 0.973 to 0.971. governing_law stays at F1 1.0 under one canonical name. OCR, tables, validation, review, multi-document queries and security are unchanged; the 8 re-formatted numbers had no metric effect (numbers are compared as Decimals).
