jake — 02/12/2025, 18:22
I personally wouldn't merge it and dae is even stricter than I am on these types of things
Cheesecake — 02/12/2025, 18:22
But even in that situation, the end result is only a slightly-sub-optimal parameter fitting.
Expertium

— 02/12/2025, 18:22
Jake, I'm surprised that you are concerned about this, given that your usual stance is "let's make things better for everyone who is not a complete lunatic, and let complete lunatics suffer"
Cheesecake — 02/12/2025, 18:23
By enabling AO to run and run at the correct times, there will be way more closeness to optimal fitting over the long run for almost all users.
jake — 02/12/2025, 18:23
not syncing every day is not necessarily lunatic behavior
Cheesecake — 02/12/2025, 18:24
Not syncing every day is one thing. 20k and 19.9k reviews unsynced is.
Expertium

— 02/12/2025, 18:24
Not syncing 19.9k is
Cheesecake — 02/12/2025, 18:24
And like, the numbers do matter because the numbers affect how good the fit is.
jake — 02/12/2025, 18:24
"I do image occulusion on the pc but other cards on my phone" is not necessarily an unreasonable assumption of people using two devices?
again, the number is illustration
Cheesecake — 02/12/2025, 18:24
The number matters.
The number affects the goodness of the parameter fit.
Like, if its just 19 or 20, we should probably be using the default user FSRS parameters until we have about 100+ reviews for the user, anyway.
If it's 5000 reviews already done, and then an additional 19 or 20, it doesnt even matter.
jake — 02/12/2025, 18:25
"we shouldn't care if a card's dsr is a bit off, but I also need 15second resolution of r or else all is lost"
Cheesecake — 02/12/2025, 18:26
There are users out there who have enabled FSRS but never once hit the optimize button beacuse they don't know what FSRS even is, just that they were told to turn it on. Tons of them.
We can fix their problems.
jake — 02/12/2025, 18:26
thats a ux problem
Expertium

— 02/12/2025, 18:26
Well, thanks to Jarrett the latter won't be a problem (still unsure about sort orders though, but at least browser and stats will be fine)
jake — 02/12/2025, 18:27
my ideal solution: stick an "optimize" button next to sync at the top that turns red or something when its been 3 weeks since the last optimize (or some other metric)
but dae doesn't like it ¯\_(ツ)_/¯
Expertium

— 02/12/2025, 18:27
Dae doesn't like it :FeelsBadAnki:
Expertium

— 02/12/2025, 18:27
yep
Cheesecake — 02/12/2025, 18:27
I think that's an okay idea, but I'd say go even further and automate it and solve the syncing issues to go with them
But thats just me
jake — 02/12/2025, 18:28
one of these tasks is an afternoon of work, the other has been discussed for months and will continue to be discussed until the button is just added
Cheesecake — 02/12/2025, 18:28
That's a very good point.
🦙

— 02/12/2025, 18:29
i suspect this will be inevitable
jake — 02/12/2025, 18:31
noooo my phone is ancient garbage I don't want it to catch fire doing all that work
Cheesecake — 02/12/2025, 18:31
Even an iPhone 4s is fast enough to do the calculation in reasonable time.
jake — 02/12/2025, 18:31
what is a reasonable amount of time?
🦙

— 02/12/2025, 18:32
reviews from both devices get added together when syncing right? from what i understand, memory state only matters in the present when deciding the next intervals, but if you have past data on grades they're still just as useful, regardless of whether they were reviewed at inoptimal times
Cheesecake — 02/12/2025, 18:33
I was under the impression that current D, S, and FSRS parameters, and time since last review, were the only variables that affect calculating an interval.
But past reviews are useful for calculating the FSRS parameters.
🦙

— 02/12/2025, 18:35
yeah but the current memory state (D,S) is calculated recursively from what i understand
merging together two sets of reviews whose intervals were calculated from different memory states breaks that assumption
Expertium

— 02/12/2025, 18:36
You need the card's entire history to calculate DSR values, and you need FSRS parameters
🦙

— 02/12/2025, 18:37
couldnt we start from the last common revlog entry?
Expertium

— 02/12/2025, 18:37
You're gonna need to draw me a diagram because I'm not sure what is the problem
Cheesecake — 02/12/2025, 18:38
Device A) "I have X reviews when I did the FSRS parameter fitting"
Device B) "I have Y. Oh, X is more than Y. I use yours. Or X is more than Y, I use mine."
Total number of reviews seems to be the sane choice
🦙

— 02/12/2025, 18:39
what if Y is more recent
Cheesecake — 02/12/2025, 18:39
If you have shared reviews, then it's the same for both. So its only the reviews that were only on one or the other
Recency not good
See above edge case of some device sitting there way out of sync, then one day optimizes, after other one did.
You'd be pushing poorly-calibrated data.
More reviews fixes that.
Also, more reviews is probably later, as well.
More reviews is better fit. That's just maths.
sorry
The one who had more reviews at the time of the FSRS fitting
🦙

— 02/12/2025, 18:41
perhaps a debug command to toggle it? this sync conflict can still happen with manual optimisation
Expertium

— 02/12/2025, 18:41
Let's say we have review 1 -> review 2 -> review 3, and DSR values after review 3 calculated using parameters P
Then we do review 4
Then parameters change to P'
If you're asking "Can we keep old DSR values (after review 3) that were calculated using P and then calculate the next DSR values after review 4 using parameters P' "?, then NO

You can't chain together DSR values based on different parameters. Well, you can, but only if you want to watch the world burn
jake — 02/12/2025, 18:42
oh thats a whole point, sync conflicts do have a user-facing popup, and this will certainly make that happen and confuse users further
🦙

— 02/12/2025, 18:44
yeah that's the recursive part of it. but do we actually want to keep P' around? now that we have extra revlogs from the other devices, there may be a more optimal P''
Cheesecake — 02/12/2025, 18:44
Why would you ever keep P' around? You have a better fit with more fit data.
Maybe in some metadata "My current DS value was calculated using P'"
But in any amount of use for future interval calculations? Why? There's no point. That's what P is for.
Expertium

— 02/12/2025, 18:46
I wasn't trying to imply that P or P' is based on more/better data. Just pointing out that you can't chain DSR that were calculated using different params
Cheesecake — 02/12/2025, 18:46
Oh I'll imply that P is better data. If P was made with more reviews than P', it's a better fit and P should go in the trashcan.
Expertium

— 02/12/2025, 18:47
That's not the point
🦙

— 02/12/2025, 18:48
do we need the entire revlog when optimising or is it also recursive?
Expertium

— 02/12/2025, 18:48
Maybe more effort should go into convincing Dae to add an "Optimize" button next to "Sync" 😅

Like, yeah he doesn't want to, but I feel like that's still a more workable path than trying to implement AO
Expertium

— 02/12/2025, 18:48
Of course you need the entire revlog
🦙

— 02/12/2025, 18:48
ah, that's where i misunderstood
jake — 02/12/2025, 18:49
yeah but how do you handle the people who actually want to reschedule after an optimize? 🍃
🦙

— 02/12/2025, 18:50
just add a "reschedule" button next to it
Expertium

— 02/12/2025, 18:50
Just add the entire FSRS section to the main menu
🍃
jake — 02/12/2025, 18:52
how about how address fsrs is an option on a preset but magically applies to the entire collection 🍃
on the topic of not-obviousness
fsrs and sm-2 should be allowed to coexist 🍃
🦙

— 02/12/2025, 18:53
luc added a globe icon for that
its a solved problem 👍
Expertium

— 02/12/2025, 18:53
It looks like shit
Cheesecake — 02/12/2025, 18:53
Also
Like half the globe-icon buttons in the options pane dont work
Expertium

— 02/12/2025, 18:53
And there are still plenty of people asking how to apply FSRS only to some decks
Cheesecake — 02/12/2025, 18:53
I cant remember which ones, but like 2 or 3 of them just dont stay set how I set them
Expertium

— 02/12/2025, 18:54
Reschedule unchecks itself, that's intended
jake — 02/12/2025, 18:54
because two "optimze" and 'optimize with reschedule" buttons was just too confusing
Expertium

— 02/12/2025, 18:54
I don't think any other options uncheck themselves though
Cheesecake — 02/12/2025, 18:55
It's intended to have a button presented to the user that does nothing and unchecks itself when they check it? Why not... remove it?
Expertium

— 02/12/2025, 18:55
It does though. It, well, reschedules cards
It just doesn't stay on forever
Cheesecake — 02/12/2025, 18:55
It doesnt stay on for 1 second.
Expertium

— 02/12/2025, 18:55
It stays on for 1 reschedule
Cheesecake — 02/12/2025, 18:56
Mine doesnt
Mine, I click it. Hit Save. Re-open. It's unchecked.
Yep, just verified just like that
Expertium

— 02/12/2025, 18:56
Well, you have to change DR or params
No change = no rescheduling
Cheesecake — 02/12/2025, 18:57
Okay, this UX confused me.
Like, as a user, I thought it was just broken.
Expertium

— 02/12/2025, 18:58
I said this assuming that you changed DR and/or params
If you don't, then yeah, it does nothing
Cheesecake — 02/12/2025, 18:58
Yeah I feel like
as UX, it shoulds stay on until a reschedule happens
not like, until that window closes.
jake — 02/12/2025, 19:02
anki's entire preference/setting ux is a shitshow
SoundJona

— 02/12/2025, 20:18
Damn those threads go so quickly and violently out of scope and without adressing the initial question since a few day
Or have I missed what's the consensus about those warning ?
I wonder if, a user hasn't good R coverage in his data set, we shouldn't maybe also use the default params to compute things like changing drastically DR to something he hasn't
Expertium

— 02/12/2025, 20:20
How would default params help?
SoundJona

— 02/12/2025, 20:20
I mean, it might not lead to a very low decay with 0 information about how things are going around <60R
Expertium

— 02/12/2025, 20:21
Default decay is pretty low
0.1542
SoundJona

— 02/12/2025, 20:21
But @Alex also suggested in private something interesting, we could also try to find "families" of users, that the current user could be assigned to, to try to have a better feeling about what his low R could imply
Hmm right
I just don't feel super at ease with people switching to 70% DR if all their revlog is around 90%, and I don't necessarly want them to have to install SSE and check their calibration graph
Cheesecake — 02/12/2025, 20:22
How are you going to find families? If you have enough data for a family, you have enough data to fit params on that one person.
SoundJona

— 02/12/2025, 20:23
I felt for it and it was probably the worst time of my anki's experience 😛
SoundJona

— 02/12/2025, 20:23
I'll let @Alex explain maybe more in details what he had in mind when he sees this
Alex — 02/12/2025, 21:00
if for some reason a user's 100 day-1 reviews matches all other 5k users' day-1 reviews, then we can be reasonably sure how the future behavior will be distributed. If all 5k users got the same cards right with p=0.5 at 100 days in, we can be confident up to a point that our new user will also match that behavior. This is a method of extrapolating out of the measured day-1 R
Cheesecake — 02/12/2025, 21:37
Hmmmm
I see how you might be able to extrapolate, after lots of data analysis...
But like.. if you have data for up through 1 day, that should be good for extrapolating out to 2-3 days, and if you have 2-3 days, that should be good for extrapolating out 5-7 days... It's not like a day 1 user is going to need to start making 30-day intervals from day 1...
Like, without seeing just how different different users' data are, it seems like it would be premature to say whether or not this would be superior to just... having some general "new user profile" that works for up to say, the first 1k reviews or so.
Alex — 02/12/2025, 21:43
Yeah it's a bit of wishful thinking, but I only brought it up as a way to show that it is theoretically possible to extrapolate outside of the measured R
(in private msg with soundjona)
Cheesecake — 02/12/2025, 21:44
Personally I like the method described in ⁠Bootstrapping (Cheesecake's ide… better. Since it like, mathematically determines the exact error associated with calculating an interval.
But it might be worth considering.
(exact statistical uncertainty)
Alex — 02/12/2025, 21:46
I'll learn of the details once you get something going & with code, for now I'll wait for you to cook
Cheesecake — 02/12/2025, 21:48
good enough
Jarrett Ye

— 03/12/2025, 02:42
😂
OK, there is still nobody discussing my simplified case.
Jarrett Ye

— 03/12/2025, 02:49
Let me elaborate it:
A user has 10 new cards in the first day.
The user rates again to all of them.
All cards are recalled in the second day (with rating = good).
How to provide a rational estimation to the initial stability of again cards? And the decay?

Jarrett Ye

— 03/12/2025, 03:13
I think this case is also relevant to @SoundJona's issue. Assuming that the user has 90% retention when the good interval of new card is 10 days. Somehow we collected some data points with interval = 20 and the predicted retention is 80% but the real retention is 70%. What should we do when the user want to switch to DR=80%?
Cheesecake — 03/12/2025, 10:27
There's something in the laws of statistics somewhere on how to handle this case. I don't remember the exact math, but I think you just throw in one failed review and one successful review at the very start. As N_reviews goes up, the one artificial data points significance approaches 0.
Ask google or ChatGPT for "How to calculate the confidence of calculating the probability of a biased coin flip with unknown probability* or something similar.
In N-dimensional space where you have a bunch of parameters affecting the success of the coin flip (i.e. success or failing a card at a given location in DSR space), it gets more complicated, but the above is the simplified base case on how to handle it.
https://math.stackexchange.com/questions/888562/confidence-interval-for-estimating-probability-of-a-biased-coin
Mathematics Stack Exchange
Confidence interval for estimating probability of a biased coin
Suppose we have a coin with an unknown probability $p$of coming up heads and that of $1-p$of coming up tails.

Now, we repeatedly flip the coin $n$times and record the results, heads turn up $X...
Image
Jarrett Ye

— 03/12/2025, 10:28
I know Laplace Smoothing. But your bootstrapping method will be smarter than it, right?
Cheesecake — 03/12/2025, 10:29
Hmmm
Actually I hadn't implemented that case yet
It actually probably does fail only in the cases of 100% fail or 100% pass.
I will have to make that fix in the future.
In actuality
It's extremely unlikely to get 100% successes or fails after 20 reviews, let alone 400.
That's like, #20 on my list of things to do on that project
Jarrett Ye

— 03/12/2025, 10:31
OK, fine. Let's say, what if we get 90% successes after 10 reviews with interval = 3 days?
Cheesecake — 03/12/2025, 10:31
It will handle that
Cheesecake — 03/12/2025, 10:32
!!! I forgot the name. Thanks for reminding me.
Hold up one second
I can teach you the algorithm real fast
One second lets change threads
Also my guests just arrived
Alex — 05/12/2025, 19:09
@SoundJona Following up on the idea of clustering for users and also being a plausible way of dealing with jarrett's problem, I tried a new model inspired by bayesian statistics

Assumptions:
A user's review results are distributed according to θ, the FSRS params.
But we don't know θ, so suppose that θ is drawn with uniform probability amongst all other 9999 users in the dataset. Specifically I take the parameters in the "result/FSRS-6-recency.jsonl" file

Then the prediction calculations are a straightforward application of bayes theorem. The resulting model keeps track of a probability distribution over the 9999 other users, specifying how likely that θ is actually that user's parameters.

Implementation details:
the same 5-way split is used, so the model's belief over users are frozen in time, even though with this formulation it is straightforward to implement a live optimization
recency is used
pruning of users is done to speed it up but it changes the results very slightly. In practice the code seems to run around 2x slower than a normal FSRS-6

The p-value for the superiority is 0.0087
Image
But how much does the probability distribution part matter? If we only take the most similar other user's params as the true FSRS params, the results become a bit worse
Image
But the "nearest user" parameters still has competitive metrics with FSRS-6, but the sample size is too small to tell for that so far
Expertium

— 05/12/2025, 19:11
I don't understand what it's doing. Like, how exactly does the bayesian part affect parameters?
Like "there's a 99% probability that these are parameters from user 7624" or whatever. Ok, so...what's next?
Alex — 05/12/2025, 19:12
yeah, so we calculate R based on all 10k-1 users and weight the 7624th user's R by 99%
and all other users would have total weight of 1%
Expertium

— 05/12/2025, 19:13
So it never does gradient descent?
Alex — 05/12/2025, 19:13
never
Expertium

— 05/12/2025, 19:13
Interesting
Alex — 05/12/2025, 19:14
the underlying idea is that we have 10k users in the dataset, surely a new user is similar to one of them
Expertium

— 05/12/2025, 19:15
Have you tried splitting the dataset into 5k-5k to see how well it performs?
Or some other split, like 9k-1k
Alex — 05/12/2025, 19:15
in this version FSRS-6-nearest-user, the probability distribution would give weight 100% to one specifc user, so the model would pretty much actually have FSRS params, just not trained by gradient descent
Image
Alex — 05/12/2025, 19:16
nah it would only be worse
Expertium

— 05/12/2025, 19:16
Yeah, but the point is to see how well it can perform on a new dataset
Alex — 05/12/2025, 19:16
I don't have a lot of computing resources rn so i don't want to do random stuff
Alex — 05/12/2025, 19:17
ok give me another dataset of 10k users
Expertium

— 05/12/2025, 19:17
Why not 5k-5k?
Alex — 05/12/2025, 19:18
the quality of the prior helps with performance, to get a better estimation we need another 10k
But idc about this problem
also 5k-5k is already hard to compare, the average log loss in these sets is already quite different
Expertium

— 05/12/2025, 19:20
I wonder if we could do this for pretrain somehow, to get good starting params only from data from first and second reviews
Alex — 05/12/2025, 19:21
we can do better actually, we don't actually split the dataset here. For example take user 1, and randomly sample 5k other users for a trial run, make many runs, etc
and for the 10k problem use sampling with replacement
Alex — 05/12/2025, 19:22
speaking of pretrain, my strongest FSRS does not use a pretrain
just deleted
i wonder how much time pretrain takes, how many epochs is it worth?
can add another epoch or increase batch size to compensate
Expertium

— 05/12/2025, 19:23
I could try, but I bet my asscheeks that log loss is going to get waaaaay worse
Like "even 30 more epochs wouldn't be enough to compensate for that" kind of worse
Alex — 05/12/2025, 19:24
Yeah, I only did it because i was implementing Reptile-like initial param optimizers and pretrain was super annoying to work with
But it turns out that it doens't really affect performance in the end considering that many other parameters can compensate
for example a parameter to shift the inflection point, a parameter that scales all S
Expertium

— 05/12/2025, 19:35
Hold on, something doesn't add up. If it searches for the best match, that means it should be at best equal to proper optimization with gradient descent, but it cannot outperform it
Alex — 05/12/2025, 19:39
no it only sees parameters that are from other users from the result/FSRS-6-recency.jsonl file
Expertium

— 05/12/2025, 19:40
If it cannot find the parameters from the user himself, then there is EVEN LESS of a reason for it to outperform optimization
Alex — 05/12/2025, 19:41
nah gradient descent can overfit
the point is that prior knowledge can provide constraints
Expertium

— 05/12/2025, 19:41
If it outperforms gradient descent, it means that parameters from user K provide a better fit to user N...that parameters from user N
Which makes 0 sense
Alex — 05/12/2025, 19:42
the parameters from result/FSRS-6-recency is also higher quality in that it is formed from larger collections overall, so especialy for the 1st split, I can use mature parameters whereas gradient descent might miss parameters that would generalize for larger collections
Alex — 05/12/2025, 19:42
overfitting is possible
Expertium

— 05/12/2025, 19:43
Hmmm
Interesting
Alex — 05/12/2025, 19:43
@Expertium like consider this problem, what would gradient descent push the params toward? consider without the stddev tensor since that is another form of a prior
iirc L2 regularization is equivalent to using a gaussian prior, and the resulting model is the maximum likelihood estimation
Alex — 05/12/2025, 19:46
And see how this user-bayes algorithm can also model this idea
SoundJona

— 05/12/2025, 20:32
I mean that's promising but isn't there a chance tht the few reps of a user make him "close" to a complete outlier that would generate very extreme parameters ?
Alex — 05/12/2025, 20:33
yeah, the algorithm is only as good as the prior
but in general with enough prior knowledge we can solve everything
SoundJona

— 05/12/2025, 20:37
Yeah what I'm thinking is, initially the problem to solve was : If a user only did 90% R, how to know how it behave for 70% R range ?

I guess, if he's lucky enough that the "closest user to him" is some guy that has such collection, it's all good ! But I wonder if in the 10k user set, there won't be also users with only 90% R review, thus maybe not really helping him
I mean, the algo sounds great
It's just the dataset that I'm questioning
Alex — 05/12/2025, 20:45
though at the same time it is reasonable to assume that users are reasonable in behavior, eg you cannot expect to satisfy a user that decides to press Again/Good in a way that encoders an image of a bird in binary and expect the algorithm to figure it out
And a definition of reasonable in behavior could be related to closeness to realistic users
