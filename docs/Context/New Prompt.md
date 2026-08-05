These are instructions for a number of upgrades to Pantheon. 

Please review the code base, and the documentation in Docs > Context in full

Create a new git branch to impliement the following upgrades, ad a version tag of this being version 2.1

Small fixes 

Remove the subnames under each name. For example, remove “memory_storage” from Memory & storage.

Remove the “N” column

Remove the “n=[x]” notation.

Change “weight_score” to “Index Weight”

Change “BUCKET” to “SUB INDEX”

The displayed list of 22 indices should be sortable by the return column 1y…1w and the new Sharpe ratio column. 

If the user click an index, the breakdown is displayed and the columns are sortable. The “sortable” arrow is clunky, replace with a more subtle wire frame arrow.

Upgrades 

The first row in the table should be the 1y..1w returns of QQQ as a benchmark. This row is not subject to sorting and remains at the top. Differentiate with a different background colour.

Below the 1y…1w index return, in the same format but smaller font display the return of the median constituent for each index, for each look back window. Note the median stock could differ between time periods. 

Before the 1y return column, add another column, which displays the 3mth Sharpe ratio. The look back window for the Sharpe ratio should be configurable in the configuration JSON. The Sharpe ratio look back window could be amended to 1y, 6m, 1m or 1w at a future point in time. 

When a user clicks an index, a drop down displays the constituents. I’d like that when a constituent is clicked, a drop down opens up with a Bloomberg style prices chart. The change will also integrate buttons corresponding to 1y … 1w, allowing the display time period to change accordingly.


Please ask clarifying questions and design choices. Highlight any new depandancies, and out put a sec for these updrages to Docs>Context.

Note, as you progress throught the build update PRGRESS_REPORT.md at sensible intervals.
