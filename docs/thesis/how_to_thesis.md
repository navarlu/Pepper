# Bc. or MSc. thesis howto @ [Humanoids](https://cyber.felk.cvut.cz/research/groups-teams/humanoids/)

[Forms](#forms)

[Latex and overleaf](#latex-and-overleaf)

[Our template](#our-template)

[Comments in .tex source](#comments-in-.tex-source)

[Macros and Acronyms](#macros-and-acronyms)

[Official template](#official-template)

[Recommended structure](#recommended-structure)

[Abstract](#abstract)

[References](#references)

[References to sections, figs, tables etc.](#references-to-sections,-figs,-tables-etc.)

[References to code](#references-to-code)

[References to dataset](#references-to-dataset)

[Formatting (latex)](#formatting-\(latex\))

[General recommendations](#general-recommendations)

[Tips for writing in English](#tips-for-writing-in-english)

[Misc tips](#misc-tips)

[Use of AI tools](#use-of-ai-tools)

[Wrap-up \- clean-up](#wrap-up---clean-up)

[Presentation](#presentation)

 

# Forms {#forms}

KYR: [https://cyber.felk.cvut.cz/teaching/dpext/Forms/navrh\_ZP.docx](https://cyber.felk.cvut.cz/teaching/dpext/Forms/navrh_ZP.docx)   
OI: [link](https://docs.google.com/forms/d/1_rncSh5wd1alythRJyqbt1W6IA8kZOVA661_-rpnAAc/edit?usp=drive_web) 

# Latex and overleaf {#latex-and-overleaf}

Use [https://www.overleaf.com/](https://www.overleaf.com/).  
Ask Matej to create an empty project for you.

## Our template {#our-template}

Our template can be found at: [https://www.overleaf.com/read/rhtrbtpktvyy](https://www.overleaf.com/read/rhtrbtpktvyy) 

- The current values are already tested for printing (Lukas)

The official template is too narrow and spaces between paragraphs are unnecessarily big.   
\- it can be edited in ctuth-pkg.tex  
\- line 202 controls the width of the page  
\- original value is 33, the current is 39 (Lukas), but can be even a bit more  
\- line 199 controls the ratio of inner/outer bounds   
\- 23:27 is okay for the width of 39   
\- lines 392-395 controls spacing before/after sections, chapters etc.  
\-  other spacing can be set in main.tex around line 125  
\- “\\setlength{\\parskip}{2.5ex plus 0.2ex minus 0.2ex}”  
\- basically, only the first number is important and controls the spacing between paragraphs, paragraphs and labels etc.  
\-   for current values in ctuth-pkg.tex 2.5ex is “visually nice”, but for more compact spaces even 0.5 can be used   
 

### Comments in .tex source {#comments-in-.tex-source}

- **In our template, line 32 (showCommentstrue) can be changed to show/hide all comments**

To enable us to comment into the text, you can adapt this macro:  
\\usepackage\[markup=bfit, deletedmarkup=sout, authormarkup=brackets\]{changes}  
%\\usepackage\[final\]{changes}  
\\definecolor{darkgreen}{RGB}{0,111,0}  
\\definechangesauthor\[name={Adrian}, color=darkgreen\]{AP}  
\\definechangesauthor\[name={Zdenek}, color=blue\]{ZS}  
\\definechangesauthor\[name={Matej}, color=orange\]{MH}  
\\newcommand{\\ap}\[1\]{\\added\[id=AP\]{\[\#1\]}}  
\\newcommand{\\zs}\[1\]{\\added\[id=ZS\]{\[\#1\]}}  
\\newcommand{\\mh}\[1\]{\\added\[id=MH\]{\[\#1\]}}

### Macros and Acronyms {#macros-and-acronyms}

Our template also includes some pre-defined macros and acronyms, that can make the work easier.

- Macros can be used to add repeating thing \- *e.g., i.e., et al.*, references to figures and tables with correct names or writing a word that repeats a lot  
  - Examples:  
    - To write Hoffmann *et al. \-\>* \\etal{Hoffmann}  
    - To reference a figure as Fig. X \-\> \\figref{name\_of\_the\_reference}  
    - To write state-of-the-art \-\> \\sotaa{}  
  - All macros are in *macro.tex* file. New can be added by users  
- Acronyms serve to avoid necessity to remember whether you already defined a given name with acronym or not  
  - When used for the first time, both long name and acronyms in brackets will be written. With all other used, only the acronym will be written  
    - First usage \\ac{ros} \-\> Robot Operating system (ROS); all other \\ac{ros} \-\> ROS  
  - Defined in *acronyms.tex* file. New can be added.

## Official template {#official-template}

Latex thesis template on overleaf: [https://www.overleaf.com/latex/templates/latex-template-for-theses-at-ctu-in-prague/pvmfzyzcdzxg](https://www.overleaf.com/latex/templates/latex-template-for-theses-at-ctu-in-prague/pvmfzyzcdzxg)  
Thesis template on github:  
[https://github.com/tohecz/ctuthesis](https://github.com/tohecz/ctuthesis)

Note: if you have strange problems compiling, make sure the following line is commented out.   
% \\ctutemplate{specification.as.chapter}

# Recommended structure {#recommended-structure}

1. Introduction  
   1. Motivation  
   2. Goals  
   3. Structure of the thesis. “The thesis is structured as follows.”  
2. Related Work  
   1. Make sure you cover those from the thesis assignment.  
   2. With every reference, seek a relation to this work.  
   3. Do not write 1 reference \- 1 paragraph. Add additional structure \- group the related work by topics. You can use subheadings.    
   4. You can add a final section “Thesis contribution” where you recap the most relevant related work and state in which ways is your work different/new.  
3. Materials and Methods  
   1. Robot setup / HW  
   2. Data processing  
   3. …  
4. Experiments and Results  
5. Conclusion  
   1. High-level summary \- what was accomplished.  
6. Discussion  
   1. What were the limitations? Be honest \- no need to be ashamed to point them out.  
   2. What are interesting implications?  
7. Future work \- what would be the next things to try? Don’t hesitate to put things you did not have time to do but wanted to.

List of Figures and Tables can be skipped.  
(20.5.2020: Matej: konzultoval jsem s pi Zichovou, se kterou jsme prohlizeli smernici [http://www.fel.cvut.cz/cz/rozvoj/smerniceSZZ.pdf](http://www.fel.cvut.cz/cz/rozvoj/smerniceSZZ.pdf), i s Tomasem Svobodou. Dospeli jsme k tomu, ze tyto seznamy jsou nepovinne a malo uzitecne. Zrejme je proste mate v templatu, ale je mozno je vypustit. Ja bych byl pro.)

# Abstract {#abstract}

Write the English abstract first and check with your supervisor.  
You can translate to Czech later. 

1. One or two sentences providing a basic introduction to the field, comprehensible to a scientist in any discipline.   
2. Two to three sentences of more detailed background, comprehensible to scientists in related disciplines.  
3. One sentence clearly stating the general problem being addressed by this particular study.  
4. One sentence summarising the main result (with the words “here we show” or their equivalent).  
5. Two or three sentences explaining what the main result reveals in direct comparison to what was thought to be the case previously, or how the main result adds to previous knowledge.  
6. One or two sentences to put the results into a more general context.

# References {#references}

You can use google scholar to look up references. However, often, the arxiv.org version pops up by default. It is not good practice to use a lot of arxiv references. For many of them, you will be able to find that they were eventually published at conference / in a journal. This is then the proper reference to cite. 

See also [Formatting (latex)](#formatting-\(latex\)) \- References

# References to sections, figs, tables etc. {#references-to-sections,-figs,-tables-etc.}

Always use dynamic references: using \\label{} and \\ref{}.   
For sections and subsections, use them frequently throughout the text. E.g., “more details can be found in Section 3.2”   
When you refer to a specific section/figure/table, you use the capital first letter. 

* “This is shown in Table\~\\ref{table:bla1}”    
* “Please refer to Section\~\\ref{section:blu1} for more details.”-  
* ...

Note. End every Figure caption with a full stop.

# References to code  {#references-to-code}

We will create a repository for you under [https://gitlab.fel.cvut.cz/body-schema](https://gitlab.fel.cvut.cz/body-schema)  
At the latest when finishing your thesis, make this repo publicly accessible.

Somewhere in text \- e.g., beginning of Methods section \- write:   
All the code used in this thesis is available at this online repository \\cite{gitlab-repo-ipalm}. 

In text: To do this …. I used the functions in the \\texttt{preprocess} folder of \\cite{gitlab-repo-ipalm}. To do …, the function \\texttt{truncate.py} was used.

In list of references:   
P. Stoudek and M. Mareš, 2020\. \[Online\]. Available: gitlab.fel.cvut.cz/body-schema/ipalm/ipalm-grasping

Depending on the stylesheet / template you’re using, you can use something like:  
In .bib:  
(\\bibliographystyle{IEEEtran})  
@misc{gitlab-repo-ipalm,  
    type      \= {software},  
    publisher \= {GitHub},  
    journal   \= {ipalm-grasping},  
    author    \= {P. Stoudek and M. Mareš},  
    url       \= {https://gitlab.fel.cvut.cz/body-schema/ipalm/ipalm-grasping},  
    year      \= {2020}  
}  
Or  
\\bibliographystyle{ieeetr}  
@misc{gitlab-repo-ipalm,  
	type  	\= {software},  
	publisher \= {GitHub},  
	journal   \= {ipalm-grasping},  
	author	\= {P. Stoudek and M. Mareš},  
	url   	\= {[https://gitlab.fel.cvut.cz/body-schema/ipalm/ipalm-grasping](https://gitlab.fel.cvut.cz/body-schema/ipalm/ipalm-grasping)},  
	year  	\= {2020},  
	howpublished \= {\[online\]. Available at \\url{[https://gitlab.fel.cvut.cz/body-schema/ipalm/ipalm-grasping](https://gitlab.fel.cvut.cz/body-schema/ipalm/ipalm-grasping)}},  
}

# References to dataset {#references-to-dataset}

We will create a folder for you on our lab google drive.   
We recommend you to use a directory structure like for example:

* data  
* videos  
* pictures  
* etc.

Somewhere, e.g. at the beginning of Methods section, you can write:  
“All data including multimedia materials used in this thesis can be accessed at \\ref{gdrive-folder}.”  
Later, you can refer to individual subfolders. E.g.: “The data from this experiment can be accessed  under \\texttt{data\\dataset1} and the video at \\texttt{video\\video1} at \\ref{gdrive-folder}. 

You may not want to give everyone access to the top-level folder where you may have other things. You may thus set access explicitly only to “data”, “videos”, etc. and then create a reference entry separately for each of those.  

In bibtex, you can use an entry like this:  
@misc{gdrive-folder,  
    type      \= {data},  
    publisher \= {Google Drive},  
    author    \= {M. Mareš},  
    url       \= {https://drive.google.com/drive/folders/1n7A32uZq4AAvE3bT4Ov9hBmlB3mqkUFV?usp=sharing},  
    year      \= {2020}  
}

# Formatting (latex) {#formatting-(latex)}

**“Quotation marks”**  
In latex, you need to type (2x ![][image1] ) \`\`text’’ (2x ![][image2], not 1x with shift)  to get “text”  
[https://www.maths.tcd.ie/\~dwilkins/LaTeXPrimer/QuotDash.html](https://www.maths.tcd.ie/~dwilkins/LaTeXPrimer/QuotDash.html)

**Dashes**  
“-” is a minus or it is the hyphen within a word \- e.g. self-touch.

To separate words not in a sentence, like in a section title, you use “--” in latex (the n-dash).  
3.2 Model 1 – Parameters

To separate words in a sentence, you can use:  
N-dash,  “ \--” in latex, is the standard dash – that you use in Czech like this – to break the sentence   
m-dash “---” is proper English to break the sentence—without spaces on either side—bla bla...  
[https://www.maths.tcd.ie/\~dwilkins/LaTeXPrimer/QuotDash.html](https://www.maths.tcd.ie/~dwilkins/LaTeXPrimer/QuotDash.html)  
Choose one of the two and be consistent.

**References**  
You cite Kalman et al.\~\\cite{Kalman} like this.  
If there is only one author, you write Kalman\~\\cite{Kalman}.  
If there are two authors, you write Hoffmann \\& Pfeifer\~\\cite{HoffmannPfeifer2018}.  
If you end a sentence with references, put them before the full stop. Like this:  
This was shown in \\cite{REF1, REF2}.  
   
In your .bib file, give your references meaningful IDs \- e.g.  
@article{dean2017integration,  
  title={Integration of robotic technologies for rapidly deployable robots},  
  author={Dean-Leon, Emmanuel and Ramirez-Amaro, Karinne and Bergner, Florian and   Dianov, Ilya and Cheng, Gordon},  
  …  
}

Or   
@inproceedings{guedelha2016self,  
  title={Self-calibration of joint offsets for humanoid robots using accelerometer measurements},  
  author={Guedelha, Nuno and Kuppuswamy, Naveen and Traversaro, Silvio and Nori, Francesco},  
  …  
}

And then \\cite{dean2017integration} ... 

Do not use the way it is done in the thesis template:  @article{cite:12, …} in .bib and then \\cite{cite:12} in .tex 

**Symbols**  
Inside the text, use \\emph{symbol} to make them stand out from the text. \\emph is preferable over italics, even if by default it will be italics. ([https://tex.stackexchange.com/questions/1980/emph-or-textit](https://tex.stackexchange.com/questions/1980/emph-or-textit)) 

**Code**  
To display pieces of code, you can use  
\\begin{listing}...

**Algorithm / pseudocode**  
You can use \\begin{algorithm}

**Units**  
The best way to use units is probably *siunitx* library. Used like \\SI{10}{\\milli\\meter}. The library adds the correct spaces between number and unit; correct typesetting *etc.*

***e.g., i.e.,***  
Both of these should be written in italics and end with comma. In our template, macros \\ie{} and \\eg{} can be used.

# General recommendations {#general-recommendations}

* Keep in mind that the reader will not be familiar with your work. Check whether the reader will easily understand everything that you wrote.   
  * See [Misc tips](#misc-tips) for some recommendations  
* Did you provide everything necessary to repeat an experiment and reproduce your results?

# Tips for writing in English {#tips-for-writing-in-english}

Take advantage of [http://knihovna.cvut.cz/podpora-vedy/publikovani/writefull-na-cvut\#writefull-for-overleaf](http://knihovna.cvut.cz/podpora-vedy/publikovani/writefull-na-cvut#writefull-for-overleaf)

* Have a look at the uses of which vs. that ([https://www.grammarly.com/blog/which-vs-that/](https://www.grammarly.com/blog/which-vs-that/)). There is no comma before a sentence starting with “that” (že in Czech).   
* “Cartesian” is spelled with capital C.  
* Do not use informal language.  
  * Do not use short forms like don’t, won’t, isn’t etc.  
* Do not start a sentence with a reference:  
  * Wrong: \[3\] show that …  
  * Correct: FirstAuthor et al. \[3\] show that   
* When referring to parts of the thesis that are numbered or named, use capital first letter: see Chapter 3 / as described in Section 3.3, Table 2.5, Figure 3….

# Misc tips {#misc-tips}

1. Structure everything as much as possible with headings, subheadings, bullet lists etc.  
   1. Do not use too short paragraphs though. 3 lines is a minimum, but even that is still short.   
2. Use a lot of schematics and pictures\! For example, show your software architecture, neural network… etc. using a schematics. This can tell more than a 1000 words \- especially given that your writing may not be so great….   
3. If you’re controlling a robot or similar in a closed loop, consider using a block diagram (“blokové schéma”) \- inputs, control actions, outputs…   
4. Consider a video attachment to your thesis \- as part of [References to dataset](#references-to-dataset)  for example.

# Use of AI tools {#use-of-ai-tools}

* [https://www.cvut.cz/sites/default/files/content/d1dc93cd-5894-4521-b799-c7e715d3c59e/en/20231003-methodological-guideline-no-52023.pdf](https://www.cvut.cz/sites/default/files/content/d1dc93cd-5894-4521-b799-c7e715d3c59e/en/20231003-methodological-guideline-no-52023.pdf)   
* [https://cw.fel.cvut.cz/wiki/help/ai\_in\_education/start](https://cw.fel.cvut.cz/wiki/help/ai_in_education/start) 

In KOS it is necessary to acknowledge the use of AI when submitting your work.

# Wrap-up \- clean-up {#wrap-up---clean-up}

When you finish your studies \- after the state exam \- your FEE/CTU accounts (google drive, gitlab, ...) may quickly be closed or even deleted. Before you go to the defense, consolidate your files and transfer them to Matej.   
Here is a how to do it video: [link](https://drive.google.com/file/d/104xrLMjITMEWU00V04FGPw0g7jUqaDuA/view?usp=share_link)   
 

# Presentation {#presentation}

* Use slide numbers  
* Show things graphically rather than explain using words.  
* If you’re controlling a robot or similar in a closed loop, consider using a block diagram (“blokové schéma”) \- inputs, control actions, outputs… 

[image1]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAB4AAAAgCAIAAACKIl8oAAACj0lEQVR4Xu2Wu4oiQRSGS0fb67jrHRQEmdBMvAVm6229jIrGgjIwCOZGaiKaiIG+gAq+gJMNA/MGPoCxmaaC4G3/7p52m3JxHU39wO6q6lP/qT5lnT6EEPL4+KjX62Ox2G8RMYFEIpFMJjESiUTi8XgoFAqHw2IDEI1GQ79CuNpsNoVCodFoZDIZMZlMDoej0+nY7fafIn4IGAwGs9kMM4vFwj8yGo1YitVqPdpgEF0YVCoVuJdIJHK5nOD3NnljnZxFq9US7v3UajUamMUwDGWDxWKw0WjAB9t3u91wSxkBeH54eIBiv99fLpf7/f719VWlUmHk6emp1WoNBgN6jkChUGA1PR4Pu3hCTlcBlWw2u16vx+NxLpdDo1qtwgyBns/nzWaTsj9SLBbZm9fr5fuUNLrYkHa7PZ1OsVGI5vv7+2KxwEbVarXdboeXENuLKZVK7I2XhhAiIH6MsGIDPj4+hsOh0+lUKpUY2W63m81mtVp9fn6e2Z4vab/ff6pLOGc8EFWLOI4zJwE88vLygln/l0ZYlCL+Ct8ufQZqypFLpfEX/Oc4c7X0LdylKS6VRt6AXTAYnEwmfJdPDNT2irlUmnDJBEKz2exwOMBSKpXiiqNP2wlcKo014hDqdDpkbWQon8+H5KnkjihtKnCpNB5h1cgYaHS73UAggDdguFNKmwp8Q5oHcUBXygH1M1Mulb6CuzTFXZriSxrJgb0JhcfVYDrDfRzQLpfLuBJ8rVFT4bDhIDDXgmQAaRx9XFHc5PN5Ng2gBBiNRji1t0sTrjZDlYLykY0L8oPL5er1evBGz/gOWBxyYb1eT6VSSLxsQOANscYDVLrpdDrOgcKXb5wnISKTyTynnlHBIra8sz/9UMpUntrbfAAAAABJRU5ErkJggg==>

[image2]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACIAAAAiCAIAAAC1JZyVAAAC1klEQVR4Xu2WPU9iQRSGLx8XV1AEFROjlTEmFmgksTMxNBSWdAtK5T+QWEmlNmtFIxUdVtYaKzfxLxhbjCYWdBATTYTgPnfOxkwGL+wldutb3MycOWfeOR93zlgTExObm5vn5+eRSMS27ZGREdsdgUDAr+FjainoS8FgMBwOl8vl3d3d0dFRZ/ny4nJ6ejoUCmHG94c7MNb34mQcCyuh8WlAyLHYtlKpjI2NWXizs7ODARPZK+QOw7lYLDY+Po4tS2wtZAKhwY9MJjM7O2sxSSQSQQ26tgDhwsJCq9XqdDpzc3O5XK7b7d7X7009DfjKzgw4xOrq6mAavER1b2+v3W4fHByg3Gw26/U6xIamDgkdAzK0srIymEaE1Wr17u6OYiGlLy8vh4eHHylxg1+VBnFLJpMDaCSZk5OTj4+PxAqbh4cHIoacrOiavWBnmKiR5eVlZ96fhi+pfn5+pliIXqPRuL29RR6Px3VNAyhIeqiOf6LhOMRXbKR8GUNplJYBbzSW8l3kDPAGezH++9O5wDMNfxLFFlBgbKka7f1RDHimGQ7fNEPgm2YI/Oc03GN+daWzxAXzW8HQ6YVnGksR2KoVHh0dvb+/39zccD1Ho1FTT4NnGrRxQrrO1dXV6+vr1tYWu8zMzBiaOoahkbgVi8Wnp6dsNosllBJJN3imsVWb4W6u1Wpvb2/z8/NB9Qbr3z1NGkZTU1NYSsv6tMOL9uLi4traGnw0U3JDOzD1NHimkTefpRoPBLI7rvg89ZuBNCKBI6DaMwNh+mIa0mArsORTTwMsEYqLbjBpcJ+yEQIK99MSwMCnXlL9PdDhVw95XKe+lpaWHNHZ2RkpRRpR8A0LnUYCwLlLpRJMjjfpdPr6+ho9pM7r/YtAvZyenu7v7zsTQsz8+Ph4Y2MjriA+DYGwBo6bSqVOfp3wGziJIHziIE/vQqGwvb398yuQz+fX19fhljfXH+Evul6Ptl9pAAAAAElFTkSuQmCC>